#!/usr/bin/env python3
"""Post social media updates from blog post front matter."""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

POSTS_DIR = Path(__file__).resolve().parent.parent / "_posts"
STATE_FILE = Path(__file__).resolve().parent / "social_posted.json"
SITE_URL = os.environ.get("SITE_URL", "https://shlema.me")

PLATFORMS = ["x", "bluesky", "mastodon", "threads"]


# --- Front matter parsing ---

def parse_front_matter(filepath: Path) -> dict | None:
    text = filepath.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?\n)---", text, re.DOTALL)
    if not match:
        return None
    return yaml.safe_load(match.group(1))


def get_social_fields(front_matter: dict) -> dict[str, str]:
    return {
        p: front_matter[f"social_{p}"]
        for p in PLATFORMS
        if f"social_{p}" in front_matter and front_matter[f"social_{p}"]
    }


# --- State management ---

def load_state() -> dict:
    if STATE_FILE.exists():
        content = STATE_FILE.read_text()
        if content.strip():
            return json.loads(content)
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def platforms_to_post(filename: str, social_fields: dict, state: dict) -> dict[str, str]:
    """Return {platform: text} for platforms that need posting."""
    entry = state.get(filename, {})
    posted = entry.get("platforms", {})
    result = {}
    for platform, text in social_fields.items():
        status = posted.get(platform, {}).get("status")
        if status != "success":
            result[platform] = text
    return result


# --- Platform posting ---

def post_to_x(text: str) -> str:
    import tweepy
    client = tweepy.Client(
        consumer_key=os.environ["X_CONSUMER_KEY"],
        consumer_secret=os.environ["X_CONSUMER_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
    )
    response = client.create_tweet(text=text)
    return str(response.data["id"])


def post_to_bluesky(text: str) -> str:
    from atproto import Client, IdResolver, models
    client = Client()
    client.login(os.environ["BLUESKY_HANDLE"], os.environ["BLUESKY_APP_PASSWORD"])
    resolver = IdResolver()

    facets = []
    text_bytes = text.encode("utf-8")

    # URL facets
    for match in re.finditer(r"https?://[^\s]+", text):
        url = match.group()
        start = len(text[:match.start()].encode("utf-8"))
        end = start + len(url.encode("utf-8"))
        facets.append(
            models.AppBskyRichtextFacet.Main(
                index=models.AppBskyRichtextFacet.ByteSlice(
                    byte_start=start, byte_end=end,
                ),
                features=[models.AppBskyRichtextFacet.Link(uri=url)],
            )
        )

    # Mention facets — resolve @handle.bsky.social to DID
    for match in re.finditer(r"@([\w.-]+\.[\w.-]+)", text):
        handle = match.group(1)
        try:
            did = resolver.handle.resolve(handle)
        except Exception as e:
            logger.warning("Could not resolve Bluesky handle @%s: %s", handle, e)
            continue
        start = len(text[:match.start()].encode("utf-8"))
        end = start + len(match.group().encode("utf-8"))
        facets.append(
            models.AppBskyRichtextFacet.Main(
                index=models.AppBskyRichtextFacet.ByteSlice(
                    byte_start=start, byte_end=end,
                ),
                features=[models.AppBskyRichtextFacet.Mention(did=did)],
            )
        )

    response = client.send_post(text, facets=facets or None)
    return response.uri


def post_to_mastodon(text: str) -> str:
    from mastodon import Mastodon
    client = Mastodon(
        access_token=os.environ["MASTODON_ACCESS_TOKEN"],
        api_base_url=os.environ["MASTODON_INSTANCE_URL"],
    )
    status = client.status_post(text)
    return str(status["id"])


def post_to_threads(text: str) -> str:
    import requests
    access_token = os.environ["THREADS_ACCESS_TOKEN"]
    user_id = os.environ["THREADS_USER_ID"]
    base = "https://graph.threads.net/v1.0"

    # Step 1: Create container
    resp = requests.post(
        f"{base}/{user_id}/threads",
        params={"media_type": "TEXT", "text": text, "access_token": access_token},
        timeout=30,
    )
    resp.raise_for_status()
    container_id = resp.json()["id"]

    time.sleep(2)

    # Step 2: Publish
    resp = requests.post(
        f"{base}/{user_id}/threads_publish",
        params={"creation_id": container_id, "access_token": access_token},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["id"]


POSTERS = {
    "x": post_to_x,
    "bluesky": post_to_bluesky,
    "mastodon": post_to_mastodon,
    "threads": post_to_threads,
}


# --- Main ---

def run():
    state = load_state()

    post_files = sorted(POSTS_DIR.glob("*.md"))
    if not post_files:
        logger.info("No posts found in %s", POSTS_DIR)
        return

    anything_posted = False

    for filepath in post_files:
        filename = filepath.name
        front_matter = parse_front_matter(filepath)
        if not front_matter:
            continue

        social_fields = get_social_fields(front_matter)
        if not social_fields:
            continue

        to_post = platforms_to_post(filename, social_fields, state)
        if not to_post:
            continue

        logger.info("Processing: %s (%s)", front_matter.get("title", filename), list(to_post.keys()))

        entry = state.setdefault(filename, {
            "title": front_matter.get("title", ""),
            "platforms": {},
        })
        entry["title"] = front_matter.get("title", "")

        for platform, text in to_post.items():
            poster = POSTERS.get(platform)
            if not poster:
                continue
            try:
                post_id = poster(text)
                logger.info("Posted to %s: %s", platform, post_id)
                entry["platforms"][platform] = {
                    "status": "success",
                    "post_id": str(post_id),
                    "posted_at": datetime.now(timezone.utc).isoformat(),
                }
                anything_posted = True
            except Exception as e:
                logger.error("Failed to post to %s: %s", platform, e)
                entry["platforms"][platform] = {
                    "status": "failed",
                    "error": str(e),
                }

        save_state(state)

    if not anything_posted:
        logger.info("Nothing new to post.")


if __name__ == "__main__":
    run()
