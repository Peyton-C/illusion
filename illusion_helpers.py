import discord
import subprocess

from PIL import Image
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