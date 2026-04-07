Jekyll::Hooks.register [:pages, :documents], :post_render do |item|
  item.output = item.output.gsub(/==([^=]+)==/, '<mark>\1</mark>')
end
