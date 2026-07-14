import discord

def format_quantity(value):
    if value is None:
        return "N/A"

    value = float(value)

    return f"{value:g}"


def make_low_thread_content(item):
    stock_lines = []

    if item["TRACKING_MODE"] != "KANBAN":
        stock_lines.extend(
            [
                f"Current Stock: {format_quantity(item['QUANTITY_ON_HAND'])} "
                f"{item['UNIT'] or ''}".strip(),
                f"Low Threshold: {format_quantity(item['LOW_THRESHOLD'])} "
                f"{item['UNIT'] or ''}".strip(),
            ]
        )

    return "\n".join(
        [
            f"We are getting low on: {item['NAME']}",
            f"SKU: {item['SKU']}",
            f"Tracking Mode: {item['TRACKING_MODE']}",
            f"Priority: {item['PRIORITY']}",
            f"Order Quantity: {item['ORDER_QUANTITY']}",
            *stock_lines,
        ]
    )

def get_vendor_links(item):
    vendor_links = []

    for vendor_number in range(1, 6):
        vendor = item.get(f"VENDOR_{vendor_number}")
        link = item.get(f"LINK_{vendor_number}")

        if link:
            label = vendor or f"Vendor {vendor_number}"

            vendor_links.append(
                {
                    "label": label[:80],
                    "url": link,
                }
            )

    return vendor_links


def make_vendor_buttons(item):
    vendor_links = get_vendor_links(item)

    if not vendor_links:
        return None

    view = discord.ui.View()

    for vendor in vendor_links:
        # Discord doesnt allow embeded links without http:// or https://, even though thats a pretty normal thing now, but discord sucks. -PC
        if vendor["url"].startswith("http"):
            url = vendor["url"]
        else:
            url = "http://" + vendor["url"]
        view.add_item(
            discord.ui.Button(
                label=vendor["label"],
                url=url,
                style=discord.ButtonStyle.link,
            )
        )

    return view