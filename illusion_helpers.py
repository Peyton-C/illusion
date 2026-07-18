import discord
import os

from PIL import Image, ImageDraw, ImageFont
from niimprint import BluetoothTransport, PrinterClient, SerialTransport

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