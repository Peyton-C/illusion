from barcode import Code128
from barcode.writer import ImageWriter

def generate_barcode(text: str, output_file: str = "barcode") -> str:
    barcode = Code128(text, writer=ImageWriter())
    filename = barcode.save(output_file)

    return filename