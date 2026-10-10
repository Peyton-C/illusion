from PIL import Image

from niimprint import PrinterClient, SerialTransport


# Max printable width in pixels, per model
PRINTER_MAX_WIDTH = {
    "b1": 384,
    "b18": 384,
    "b21": 384,
    "d11": 96,
    "d110": 96,
}


class PrinterUnavailable(Exception):
    """The printer cannot print right now (asleep, open, out of labels, unplugged...).

    Anything raising this is worth pausing the print queue over, since retrying
    will keep failing until a human does something about it.
    """


class LabelError(Exception):
    """The label itself is bad, the printer is fine. Only this job should be dropped."""


def connect_printer(addr):
    try:
        transport = SerialTransport(port=addr)
        printer = PrinterClient(transport)

        heartbeat = printer.heartbeat()
        media_info = printer.get_rfid()
    except Exception as e:
        err = str(e)

        if "could not open port" in err:
            raise PrinterUnavailable("printer is likely disconnected")
        elif "has no attribute 'data'" in err:
            raise PrinterUnavailable("printer is likely asleep")
        else:
            raise PrinterUnavailable(f"Unknown Error: {err}")

    return printer, transport, heartbeat, media_info


def close_printer(transport):
    # SerialTransport has no close(), and we open a fresh one for every print,
    # so reach in and close the port rather than leaking file descriptors.
    try:
        serial_port = getattr(transport, "_serial", None)

        if serial_port is not None:
            serial_port.close()
    except Exception:
        pass


def media_remaining(media_info):
    if media_info is None:
        return None

    return media_info["label_capacity"] - media_info["used_len"]


def check_printer_ready(heartbeat, media_info):
    if heartbeat["closingstate"] == 0:
        raise PrinterUnavailable("the printer seems to be open, please close it and try again")

    if media_info is None:
        raise PrinterUnavailable("no labels detected, please load a roll")

    if media_remaining(media_info) <= 0:
        raise PrinterUnavailable("no labels left, please replace roll")


def check_printer(addr):
    """Connect, verify the printer is able to print, and report on the loaded roll."""
    printer, transport, heartbeat, media_info = connect_printer(addr)

    try:
        check_printer_ready(heartbeat, media_info)
    finally:
        close_printer(transport)

    return media_info


def check_label(img, model):
    max_width = PRINTER_MAX_WIDTH.get(model)

    if max_width is None:
        return

    with Image.open(img) as image:
        if image.width > max_width:
            raise LabelError(f"image too wide, {image.width}px given, {max_width}px is the limit")


def niimbot_print(img, addr, model, density=3):
    """Print a single image. Blocking, so run it in a thread when called from the bot."""
    printer, transport, heartbeat, media_info = connect_printer(addr)

    try:
        check_printer_ready(heartbeat, media_info)
        check_label(img, model)

        image = Image.open(img)

        printer.print_image(image, density=density)
    except (PrinterUnavailable, LabelError):
        raise
    except Exception as e:
        # A failure mid print is a printer problem, so treat it like one
        raise PrinterUnavailable(f"Unknown Error: {e}")
    finally:
        close_printer(transport)


def niimbot_printer_info(addr):
    try:
        printer, transport, heartbeat, media_info = connect_printer(addr)
    except PrinterUnavailable as e:
        return f"Unable to get info, {e}"

    close_printer(transport)

    if media_info != None:
        remaining_media = media_remaining(media_info)

        return f"Labels left: {remaining_media}/{media_info["label_capacity"]}\nBattery Level: {heartbeat["powerlevel"]}/4"
    else:
        return "Unable to get printer info, labels might not be loaded."
