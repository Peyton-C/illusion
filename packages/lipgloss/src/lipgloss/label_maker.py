from io import BytesIO
from pathlib import Path
from typing import Optional
import os

from barcode import Code128
from barcode.writer import ImageWriter
import qrcode
from qrcode.constants import ERROR_CORRECT_L
from PIL import Image, ImageDraw, ImageFont, ImageOps

# A label is white on white against a Discord message, so the preview gets a
# frame to show where its edges actually are
PREVIEW_BORDER_COLOUR = "#999999"
PREVIEW_MAX_SCALE = 8


def preview_png(path, scale=3) -> bytes:
    """A rendered label blown up for a screen, framed, as PNG bytes.

    Nearest neighbour on purpose: the point of a preview is to show the pixels
    the printer is going to get, and a smooth resample would flatter a barcode
    that is really too fine to scan.
    """
    scale = max(1, min(int(scale), PREVIEW_MAX_SCALE))

    with Image.open(path) as label:
        scaled = label.convert("RGB").resize(
            (label.width * scale, label.height * scale),
            Image.Resampling.NEAREST,
        )

    framed = ImageOps.expand(scaled, border=1, fill=PREVIEW_BORDER_COLOUR)

    buffer = BytesIO()
    framed.save(buffer, format="PNG")

    return buffer.getvalue()


def flattened_for_print(data):
    """An uploaded image as the print head should see it: RGB, on white paper.

    The head is one bit per pixel and knows nothing about alpha, and niimprint
    reaches it through convert("L"), which drops the alpha channel without
    consulting it. A logo on a transparent background therefore arrives as RGB
    black across its whole canvas and burns the entire label -- the one case
    where doing nothing to an uploaded image is not the neutral choice.

    Compositing onto white first is what makes such a PNG print the way it
    looks. An image with no alpha comes through this unchanged.
    """
    with Image.open(BytesIO(data)) as source:
        image = source.convert("RGBA")

    paper = Image.new("RGB", image.size, "white")
    paper.paste(image, mask=image.getchannel("A"))

    return paper


def dithered_png(data, scale=3, rotate=0) -> bytes:
    """An uploaded image as the print head will actually lay it down.

    The printer is one bit per pixel. niimprint gets there with

        ImageOps.invert(image.convert("L")).convert("1")

    and PIL's convert("1") dithers with Floyd-Steinberg unless it is told not
    to, so a photograph prints as a field of dots. Which dots, and whether the
    result is still recognisable at 96 pixels across, is not something anyone
    can work out by looking at the original.

    That line is reproduced here rather than approximated, so the dots below are
    the dots that get printed. The second invert is only for the viewer: the
    printer's convention is that a set bit burns, and a preview where ink came
    out white would be a strange thing to look at.

    Flattened through the same helper /print/image uses, which is what keeps
    this a preview rather than a second opinion.

    rotate turns the finished dither for someone to look at, the way /preview
    renders a label unrotated for the same reason. It is applied after the
    conversion and never before: error diffusion runs left to right and top to
    bottom, so dithering a turned image would produce a genuinely different set
    of dots from the ones the printer is going to lay down. Turning the result
    moves the dots without changing which ones they are.
    """
    printed = ImageOps.invert(flattened_for_print(data).convert("L")).convert("1")

    # Back through L because ImageOps.invert has nothing to say about mode "1".
    visible = ImageOps.invert(printed.convert("L"))

    # transpose rather than rotate: these are the four exact quarter turns, and
    # they move pixels without going near a resampling filter that could smear
    # a one bit image.
    turns = {
        90: Image.Transpose.ROTATE_90,
        180: Image.Transpose.ROTATE_180,
        270: Image.Transpose.ROTATE_270,
    }

    if rotate % 360 in turns:
        visible = visible.transpose(turns[rotate % 360])

    # Nearest neighbour for the same reason preview_png uses it, only more so:
    # a smooth resample would average the dither back into the grey it was made
    # out of, which is precisely the thing being checked.
    scale = max(1, min(int(scale), PREVIEW_MAX_SCALE))
    scaled = visible.convert("RGB").resize(
        (visible.width * scale, visible.height * scale),
        Image.Resampling.NEAREST,
    )

    framed = ImageOps.expand(scaled, border=1, fill=PREVIEW_BORDER_COLOUR)

    buffer = BytesIO()
    framed.save(buffer, format="PNG")

    return buffer.getvalue()


LABEL_STYLES = {
    "slim_barcode": {
        "columns": [1, 25], # Values > 1 directly map to pixels
        "rows": [1],
        "cells": [
            {"type": "barcode", "value": "sku", "col": 0,"row": 0,},
            {"type": "text", "value": "sku", "col": 1, "row": 0, "rotate": 90,},
        ],
    },

    "label_barcode": {
        "columns": [1, 25],
        "rows": [0.5, 0.5],
        "cells": [
            {"type": "text", "value": "input_text_1", "col": 0, "row": 0,},
            {"type": "barcode", "value": "sku", "col": 0, "row": 1,},
            {"type": "text", "value": "sku", "col": 1, "row": 0, "rowspan": 2, "rotate": 90,},
        ],
    },

    "label_1_line": {
        "columns": [1],
        "rows": [1],
        "cells": [
            {"type": "text", "value": "input_text_1", "col": 0, "row": 0,},
        ],
    },

    "label_2_line": {
        "columns": [1],
        "rows": [0.5, 0.5],
        "cells": [
            {"type": "text", "value": "input_text_1", "col": 0, "row": 0,},
            {"type": "text", "value": "input_text_2", "col": 0, "row": 1,},
        ],
    },

    "classic_barcode": {
        "columns": [1], # Values > 1 directly map to pixels
        "rows": [0.8, 0.2],
        "cells": [
            {"type": "barcode", "value": "sku", "col": 0,"row": 0,},
            {"type": "text", "value": "sku", "col": 0, "row": 1,},
        ],
    },

    "cable_label": {
        "columns": [0.3, 0.4, 0.3],
        "rows": [1],
        "cells": [
            {"type": "text", "value": "input_text_1", "col": 0, "row": 0, "rotate": 270, "wrap": True,},
            {"type": "text", "value": "input_text_2", "col": 2, "row": 0, "rotate": 90, "wrap": True,},
        ],
    },

    "cable_label_sku": {
        "columns": [25, 0.3, 0.4, 0.3, 25],
        "rows": [1],
        "cells": [
            {"type": "text", "value": "sku", "col": 0, "row": 0, "rotate": 270,},
            {"type": "text", "value": "input_text_1", "col": 1, "row": 0, "rotate": 270, "wrap": True,},
            {"type": "text", "value": "input_text_2", "col": 3, "row": 0, "rotate": 90, "wrap": True,},
            {"type": "text", "value": "sku", "col": 4, "row": 0, "rotate": 90,},
        ],
    },

    "cable_label_qr": {
        "columns": [0.4, 0.3, 0.3, 25],
        "rows": [1],
        "cells": [
            {"type": "text", "value": "input_text_1", "col": 0, "row": 0, "rotate": 270, "wrap": True,},
            {"type": "qr", "value": "sku", "col": 2, "row": 0,},
            {"type": "text", "value": "sku", "col": 3, "row": 0, "rotate": 90,},
        ],
    },

    "label_1_line_qr": {
        "columns": [0.7, 0.3, 25],
        "rows": [1],
        "cells": [
            {"type": "text", "value": "input_text_1", "col": 0, "row": 0, "wrap": True,},
            {"type": "qr", "value": "sku", "col": 1, "row": 0,},
            {"type": "text", "value": "sku", "col": 2, "row": 0, "rotate": 90,},
        ],
    },

    "label_2_line_qr": {
        "columns": [0.7, 0.3, 25],
        "rows": [0.5, 0.5],
        "cells": [
            {"type": "text", "value": "input_text_1", "col": 0, "row": 0,},
            {"type": "text", "value": "input_text_2", "col": 0, "row": 1,},
            {"type": "qr", "value": "sku", "col": 1, "row": 0, "rowspan": 2,},
            {"type": "text", "value": "sku", "col": 2, "row": 0, "rotate": 90, "rowspan": 2,},
        ],
    },
}

# Wire names for the values a style pulls in, so a rejection can name the field
# the caller actually sent rather than the one the grid calls it
STYLE_FIELD_NAMES = {
    "sku": "sku",
    "input_text_1": "line_1",
    "input_text_2": "line_2",
}


def missing_values(style_name, values):
    """The fields a style needs that came in empty, named as the caller sent them.

    Without this a style whose text is missing dies inside PIL with a TypeError
    about NoneType, which reaches the user as a 500 from a service that is
    perfectly healthy.
    """
    missing = []

    for cell in LABEL_STYLES[style_name]["cells"]:
        key = cell["value"]
        name = STYLE_FIELD_NAMES.get(key, key)

        if not values.get(key) and name not in missing:
            missing.append(name)

    return missing


class LabelMaker:
    def __init__(self, font):
        self._font = font
        self._min_font_size = 6
        self._max_font_size = 96
        self._padding = 2

        # niimbot d110 specific stuff
        self._dpi = 203
        self._px_per_mm = self._dpi / 25.4
    
    def _text(self, text, width, height, wrap=False, line_spacing=0.15,) -> Image.Image:
        temp_image = Image.new("RGB", [width, height], "white")
        draw = ImageDraw.Draw(temp_image)

        def text_size(value, font):
            bbox = draw.textbbox((0, 0), value, font=font)
            left, top, right, bottom = bbox

            return {
                "bbox": bbox,
                "width": right - left,
                "height": bottom - top,
            }

        def break_long_word(word, font, max_width):
            chunks = []
            current = ""

            for char in word:
                candidate = current + char

                if text_size(candidate, font)["width"] <= max_width:
                    current = candidate
                else:
                    if current:
                        chunks.append(current)

                    current = char

            if current:
                chunks.append(current)

            return chunks

        def wrap_text(value, font, max_width):
            if not wrap:
                return value.splitlines() or [""]

            lines = []

            for paragraph in value.splitlines() or [""]:
                words = paragraph.split()

                if not words:
                    lines.append("")
                    continue

                current_line = ""

                for word in words:
                    if current_line:
                        candidate = f"{current_line} {word}"
                    else:
                        candidate = word

                    if text_size(candidate, font)["width"] <= max_width:
                        current_line = candidate
                        continue

                    if current_line:
                        lines.append(current_line)
                        current_line = ""

                    if text_size(word, font)["width"] <= max_width:
                        current_line = word
                    else:
                        chunks = break_long_word(word, font, max_width)

                        if chunks:
                            lines.extend(chunks[:-1])
                            current_line = chunks[-1]

                if current_line:
                    lines.append(current_line)

            return lines

        def measure_layout(font_size):
            selected_font = self._load_font(font_size)
            spacing = int(font_size * line_spacing)
            lines = wrap_text(text, selected_font, width)
            block = "\n".join(lines)

            bbox = draw.multiline_textbbox(
                (0, 0),
                block,
                font=selected_font,
                spacing=spacing,
                align="center",
            )

            left, top, right, bottom = bbox

            return {
                "font": selected_font,
                "font_size": font_size,
                "spacing": spacing,
                "lines": lines,
                "block": block,
                "bbox": bbox,
                "width": right - left,
                "height": bottom - top,
            }

        def font_fits(font_size):
            layout = measure_layout(font_size)

            return layout["width"] <= width and layout["height"] <= height

        low = self._min_font_size
        high = self._max_font_size
        best_size = self._min_font_size

        while low <= high:
            mid = (low + high) // 2

            if font_fits(mid):
                best_size = mid
                low = mid + 1
            else:
                high = mid - 1

        layout = measure_layout(best_size)

        image = Image.new("RGB", [width, height], "white")
        draw = ImageDraw.Draw(image)

        left, top, right, bottom = layout["bbox"]

        x = (width - layout["width"]) / 2 - left
        y = (height - layout["height"]) / 2 - top

        draw.multiline_text((x, y), layout["block"], font=layout["font"], fill="black", spacing=layout["spacing"], align="center",)

        return image
    
    def _barcode(self, text, width, height) -> Image.Image:
        barcode = Code128(text, writer=ImageWriter())

        modules = len(barcode.build()[0])
        quiet_modules = 10
        total_modules = modules + quiet_modules * 2

        module_px = width / total_modules
        module_width_mm = module_px / self._px_per_mm
        quiet_zone_mm = quiet_modules * module_width_mm

        vertical_margin_px = 6
        barcode_height_px = height - vertical_margin_px * 2
        module_height_mm = barcode_height_px / self._px_per_mm

        options = {
            "dpi": self._dpi,
            "module_width": module_width_mm,
            "module_height": module_height_mm,
            "quiet_zone": quiet_zone_mm,
            "write_text": False,
            "margin_top": vertical_margin_px / self._px_per_mm,
            "margin_bottom": vertical_margin_px / self._px_per_mm,
            "background": "white",
            "foreground": "black",
        }

        buffer = BytesIO()
        barcode.write(buffer, options)
        buffer.seek(0)

        image = Image.open(buffer).convert("RGB")

        return image.resize((width, height), Image.Resampling.NEAREST)

    def _qr(self, text, width, height) -> Image.Image:
        qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_L, box_size=10, border=1,)

        qr.add_data(text)
        qr.make(fit=True)

        qr_image = qr.make_image(fill_color="black", back_color="white",).convert("RGB")

        size = max(1, min(width, height))

        qr_image = qr_image.resize(
            (size, size),
            Image.Resampling.NEAREST,
        )

        image = Image.new("RGB", (width, height), "white")

        x = (width - size) // 2
        y = (height - size) // 2

        image.paste(qr_image, (x, y))

        return image

    def _load_font(self, font_size: int) -> ImageFont.ImageFont:
        if self._font is not None and self._font != "":
            return ImageFont.truetype(self._font, font_size)
        else:
            return ImageFont.load_default()

    def _png_filename(self, output_file: str) -> str:
        os.makedirs("/tmp/illusion/imgs/", exist_ok=True)

        output_path = os.path.join(
            "/tmp/illusion/imgs/",
            f"{output_file}",
        )

        if not output_path.endswith(".png"):
            output_path = f"{output_path}.png"

        return str(output_path)

    def _resolve_tracks(self, tracks, total_size):
        fixed_total = sum(track for track in tracks if track > 1)
        flexible_total = sum(track for track in tracks if track <= 1)

        remaining = total_size - fixed_total

        sizes = []

        for track in tracks:
            if track > 1:
                sizes.append(int(track))
            else:
                sizes.append(int(remaining * (track / flexible_total)))

        diff = total_size - sum(sizes)

        if sizes:
            sizes[-1] += diff

        return sizes

    def _grid_rect(self, style, width, height, cell):
        columns = self._resolve_tracks(style["columns"], width)
        rows = self._resolve_tracks(style["rows"], height)

        col = cell["col"]
        row = cell["row"]
        colspan = cell.get("colspan", 1)
        rowspan = cell.get("rowspan", 1)

        x = sum(columns[:col])
        y = sum(rows[:row])
        w = sum(columns[col : col + colspan])
        h = sum(rows[row : row + rowspan])

        return x, y, w, h

    def render_label(self, style_name, width, height, output="label", rotate=90, **values,):
        style = LABEL_STYLES[style_name]

        label = Image.new("RGB", (width, height), "white")

        for cell in style["cells"]:
            x, y, w, h = self._grid_rect(style, width, height, cell)

            padded_x = x + self._padding
            padded_y = y + self._padding
            padded_w = max(1, w - self._padding * 2)
            padded_h = max(1, h - self._padding * 2)

            value_key = cell["value"]

            if value_key not in values:
                raise ValueError(f"Missing value for label field: {value_key}")

            value = values[value_key]

            if cell["type"] == "text":
                wrap = cell.get("wrap", False)
                if cell.get("rotate") in [90, 270]:
                    image = self._text(value, padded_h, padded_w, wrap=wrap)
                else:
                    image = self._text(value, padded_w, padded_h, wrap=wrap)
            elif cell["type"] == "barcode":
                image = self._barcode(value, padded_w, padded_h)
            elif cell["type"] == "qr":
                image = self._qr(value, padded_w, padded_h)
            else:
                raise ValueError(f"Unsupported label cell type: {cell['type']}")

            if cell.get("rotate"):
                image = image.rotate(cell["rotate"], expand=True)

            paste_x = padded_x + (padded_w - image.width) // 2
            paste_y = padded_y + (padded_h - image.height) // 2

            label.paste(image, (paste_x, paste_y))

        if rotate:
            label = label.rotate(rotate, expand=True)

        output_path = self._png_filename(output)
        label.save(output_path)

        return output_path