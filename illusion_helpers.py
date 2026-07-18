import discord
import os

from PIL import Image, ImageDraw, ImageFont
from niimprint import BluetoothTransport, PrinterClient, SerialTransport

FIELD_NAMES = {
    "SKU": "SKU",
    "NAME": "Name",
    "PRIORITY": "Priority",
    "ORDER_QUANTITY": "Order Qty",
    "TRACKING_MODE": "Tracking Mode",
    "QUANTITY_ON_HAND": "Quantity",
    "LOW_THRESHOLD": "Low Threshold",
    "LOW_THREAD_ID": "Low Thread",
    "UNIT": "Unit",
    "DECREASE_AMOUNT": "Decrease By",
    "LOW": "Low",
    "VENDOR_1": "Vendor 1",
    "LINK_1": "Link 1",
    "VENDOR_2": "Vendor 2",
    "LINK_2": "Link 2",
    "VENDOR_3": "Vendor 3",
    "LINK_3": "Link 3",
    "VENDOR_4": "Vendor 4",
    "LINK_4": "Link 4",
    "VENDOR_5": "Vendor 5",
    "LINK_5": "Link 5",
}

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

def clean_sku(sku):
    if len(sku) <= 6:
        sku = "EER-" + ("0" * (6 - len(sku))) + sku
    return sku

def make_table(data, exclude=None, field_names=None):
    missing = "N/A"

    if exclude is None:
        exclude = [""]

    if field_names is None:
        field_names = FIELD_NAMES

    if isinstance(data, dict):
        rows = [data]
    else:
        rows = data

    if not rows:
        return ""

    def friendly_name(field):
        return field_names.get(field, field)

    # Vertical Table
    if len(rows) == 1:
        row = rows[0]

        field_header = "Field"
        value_header = "Value"

        table_data = []

        for field in row:
            if field not in exclude:
                value = row.get(field, missing)
                table_data.append((friendly_name(field), str(value)))

        if not table_data:
            return ""

        field_width = max(
            len(field_header),
            *(len(field) for field, _ in table_data),
        )

        value_width = max(
            len(value_header),
            *(len(value) for _, value in table_data),
        )

        header = (
            f"| {field_header.ljust(field_width)} "
            f"| {value_header.ljust(value_width)} |"
        )

        separator = f"| {'-' * field_width} | {'-' * value_width} |"

        body = []

        for field, value in table_data:
            body.append(
                f"| {field.ljust(field_width)} | {value.ljust(value_width)} |"
            )

        return "\n".join([header, separator] + body)

    # Horizontal Table
    columns = []

    for row in rows:
        for key in row:
            if key not in columns and key not in exclude:
                columns.append(key)

    if not columns:
        return ""

    string_rows = []

    for row in rows:
        string_row = {}

        for column in columns:
            string_row[column] = str(row.get(column, missing))

        string_rows.append(string_row)

    column_widths = {}

    for column in columns:
        display_column = friendly_name(column)
        max_cell_width = max(len(row[column]) for row in string_rows)
        column_widths[column] = max(len(display_column), max_cell_width)

    header_cells = []

    for column in columns:
        display_column = friendly_name(column)
        header_cells.append(display_column.ljust(column_widths[column]))

    header = "| " + " | ".join(header_cells) + " |"

    separator_cells = []

    for column in columns:
        separator_cells.append("-" * column_widths[column])

    separator = "| " + " | ".join(separator_cells) + " |"

    table_rows = []

    for row in string_rows:
        cells = []

        for column in columns:
            cells.append(row[column].ljust(column_widths[column]))

        table_rows.append("| " + " | ".join(cells) + " |")

    return "\n".join([header, separator] + table_rows)

def generate_label(line_1, line_2=None, font=None):
    lines = [line_1]
    if line_2 != None:
        lines.append(line_2)
    
    width = 320
    height = 96
    padding = 2

    usable_width = width - padding
    usable_height = height - padding

    temp_img = Image.new("RGB", [width, height])
    draw = ImageDraw.Draw(temp_img)

    def get_font(font_size):
        if font is not None and font != "":
            return ImageFont.truetype(font, font_size)
        else:
            return ImageFont.load_default()

    def measure_text(font, font_size):
        line_boxes = []

        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            left, top, right, bottom = bbox
            line_boxes.append(
                {
                    "text": line,
                    "bbox": bbox,
                    "width": right - left,
                    "height": bottom - top,
                }
            )

        line_spacing = int(font_size * 0.15) if len(lines) > 1 else 0
        total_text_height = sum(box["height"] for box in line_boxes)
        total_text_height += line_spacing * (len(lines) - 1)

        max_text_width = max(box["width"] for box in line_boxes)

        return max_text_width, total_text_height, line_boxes, line_spacing

    def font_fits(font_size):
        font = get_font(font_size)
        text_width, text_height, _, _ = measure_text(font, font_size)

        return text_width <= usable_width and text_height <= usable_height

    # Binary search for the largest font size that fits
    low = 6
    high = 96
    best_size = low

    while low <= high:
        mid = (low + high) // 2

        if font_fits(mid):
            best_size = mid
            low = mid + 1
        else:
            high = mid - 1

    font = get_font(best_size)
    _, total_text_height, line_boxes, line_spacing = measure_text(font, best_size)

    img = Image.new("RGB", [width, height], "white")
    draw = ImageDraw.Draw(img)

    current_y = (height - total_text_height) / 2

    for box in line_boxes:
        text = box["text"]
        left, top, right, bottom = box["bbox"]

        text_width = box["width"]
        text_height = box["height"]

        x = (width - text_width) / 2 - left
        y = current_y - top

        draw.text((x, y), text, font=font, fill="black")

        current_y += text_height + line_spacing

    os.makedirs("/tmp/illusion/imgs/", exist_ok=True)

    output_path = os.path.join(
        "/tmp/illusion/imgs/",
        f"label.png",
    )
    
    img = img.rotate(90, expand=True)
    img.save(output_path)
    return output_path

def niimbot_print(img, addr, model):
    try:
        transport = SerialTransport(port=addr)
        printer = PrinterClient(transport)

        heartbeat = printer.heartbeat()
        media_info = printer.get_rfid()
    except Exception as e:
        err = str(e)

        if "could not open port" in err:
            return "Unable to print, printer is likely disconnected"
        elif "AttributeError: 'NoneType' object has no attribute 'data'" in err:
            return "Unable to print, printer is likely asleep"
        else:
            return f"Unable to print, Unknown Error: {err}"
    remaining_media = media_info["total_len"] - media_info["used_len"]

    if heartbeat["closingstate"] == 0:
        return "Unable to print, The printer seems to be open, please close it and try again."
    if remaining_media == 0:
        return "No labels left, please replace roll!"
    
    if model in ("b1", "b18", "b21"):
        max_width = 384
    elif model in ("d11", "d110"):
        max_width = 96

    image = Image.open(img)

    if image.width > max_width:
        return "Unable to print, image too wide"
    
    printer.print_image(image, density=3)
    return f"Printing...\nif this is the first print after returning from sleep it may be blank."
    

def niimbot_printer_info(addr):
    try:
        transport = SerialTransport(port=addr)
        printer = PrinterClient(transport)

        heartbeat = printer.heartbeat()
        media_info = printer.get_rfid()
    except Exception as e:
        if "could not open port" in e.stderr:
            return "Unable to get info, printer is likely disconnected"
        elif "AttributeError: 'NoneType' object has no attribute 'data'" in e:
            return "Unable to get info, printer is likely asleep"
        else:
            return f"Unable to get info, Unknown Error: {e}"
        
    if media_info != None:
        remaining_media = media_info["total_len"] - media_info["used_len"]

        return f"Labels left: {remaining_media}/{media_info["total_len"]}\nBattery Level: {heartbeat["powerlevel"]}/4"
    else:
        return "Unable to get printer info, labels might not be loaded."