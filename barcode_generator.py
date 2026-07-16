from io import BytesIO
from pathlib import Path
from typing import Optional

from barcode import Code128
from barcode.writer import ImageWriter
from PIL import Image, ImageDraw, ImageFont


def generate_barcode(text: str, output_file: str = "barcode") -> str:
    barcode = Code128(text, writer=ImageWriter())
    filename = barcode.save(output_file)

    return filename

def generate_barcode_niimbot(text: str, output_file: str = "barcode", text_side: str = "left", font_path: Optional[str] = None, font_size: int = 12, text_box_width: int = 44, text_rotation: str = "cw",) -> str:
    if text_side not in {"left", "right"}:
        raise ValueError("text_side must be 'left' or 'right'")

    if text_rotation not in {"cw", "ccw"}:
        raise ValueError("text_rotation must be 'cw' or 'ccw'")

    label_w = 320
    label_h = 96

    gap = 0
    barcode_w = label_w - text_box_width - gap
    barcode_h = label_h

    label = Image.new("RGB", (label_w, label_h), "white")

    if text_side == "left":
        text_x = 0
        barcode_x = text_box_width + gap
    else:
        barcode_x = 0
        text_x = barcode_w + gap

    barcode_img = _render_code128(text=text, width=barcode_w, height=barcode_h)

    label.paste(barcode_img, (barcode_x, 0))

    _draw_rotated_text(image=label, text=text, box=(text_x, 0, text_x + text_box_width, label_h), font_path=font_path, font_size=font_size, rotation=text_rotation,)

    label = label.convert("1")
    label = label.rotate(90, expand=True)

    output_path = _png_filename(output_file)
    label.save(output_path)

    return output_path

def _render_code128(text: str, width: int, height: int) -> Image.Image:
    barcode = Code128(text, writer=ImageWriter())

    dpi = 203
    px_per_mm = dpi / 25.4

    modules = len(barcode.build()[0])
    quiet_modules = 10
    total_modules = modules + quiet_modules * 2

    module_px = width / total_modules
    module_width_mm = module_px / px_per_mm
    quiet_zone_mm = quiet_modules * module_width_mm

    vertical_margin_px = 6
    barcode_height_px = height - vertical_margin_px * 2
    module_height_mm = barcode_height_px / px_per_mm

    options = {
        "dpi": dpi,
        "module_width": module_width_mm,
        "module_height": module_height_mm,
        "quiet_zone": quiet_zone_mm,
        "write_text": False,
        "margin_top": vertical_margin_px / px_per_mm,
        "margin_bottom": vertical_margin_px / px_per_mm,
        "background": "white",
        "foreground": "black",
    }

    buffer = BytesIO()
    barcode.write(buffer, options)
    buffer.seek(0)

    image = Image.open(buffer).convert("RGB")

    return image.resize((width, height), Image.Resampling.NEAREST)

def _draw_rotated_text(image: Image.Image, text: str, box: tuple[int, int, int, int], font_path: Optional[str], font_size: int, rotation: str) -> None:
    x1, y1, x2, y2 = box
    box_w = x2 - x1
    box_h = y2 - y1

    # Draw text horizontally on a temporary canvas, then rotate it.
    temp = Image.new("RGB", (box_h, box_w), "white")
    draw = ImageDraw.Draw(temp)

    font = _load_font(font_path, font_size)

    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    x = (box_h - text_w) // 2
    y = (box_w - text_h) // 2

    draw.text((x, y), text, fill="black", font=font)

    if rotation == "cw":
        temp = temp.rotate(-90, expand=True)
    else:
        temp = temp.rotate(90, expand=True)

    image.paste(temp, (x1, y1))


def _load_font(font_path: Optional[str], font_size: int) -> ImageFont.ImageFont:
    if font_path is not None and font_path != "":
        return ImageFont.truetype(font_path, font_size)
    else:
        return ImageFont.load_default()


def _png_filename(output_file: str) -> str:
    path = Path(output_file)

    if path.suffix.lower() != ".png":
        path = path.with_suffix(".png")

    return str(path)