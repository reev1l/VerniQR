import os
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

import qrcode
from PIL import Image
from qrcode.constants import ERROR_CORRECT_M
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph


@lru_cache(maxsize=1)
def register_font() -> str:
    candidates = [
        os.environ.get("PDF_FONT_PATH", ""),
        str(Path(__file__).parent / "fonts" / "DejaVuSans.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            pdfmetrics.registerFont(TTFont("VerniQR", candidate))
            return "VerniQR"
    raise RuntimeError(
        "Не найден шрифт с кириллицей. Установите DejaVu Sans или задайте PDF_FONT_PATH."
    )


def png_label(url: str) -> bytes:
    # Keep a four-module quiet zone and use nearest-neighbor scaling (no blurred edges).
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=10, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white").get_image()
    image = image.resize((300, 300), Image.Resampling.NEAREST)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def pdf_label(url: str, name: str) -> bytes:
    from reportlab.lib.utils import ImageReader

    font = register_font()
    output = BytesIO()
    page_width, page_height = 210 * mm, 297 * mm
    pdf = canvas.Canvas(output, pagesize=(page_width, page_height))
    pdf.setTitle("Метка ВерниQR")
    pdf.setAuthor("ВерниQR")
    pdf.setFillColor(colors.black)
    pdf.setFont(font, 20)
    pdf.drawCentredString(page_width / 2, 264 * mm, "Метка ВерниQR")
    pdf.setFont(font, 10)
    pdf.drawCentredString(
        page_width / 2, 252 * mm, "Печатайте в масштабе 100%. Вырежьте метку по рамке."
    )
    left, bottom, width, height = 60 * mm, 133 * mm, 90 * mm, 105 * mm
    pdf.setStrokeColor(colors.HexColor("#bbbbbb"))
    pdf.setDash(3, 3)
    pdf.rect(left, bottom, width, height)
    pdf.setDash()
    pdf.setFont(font, 12)
    pdf.drawCentredString(page_width / 2, bottom + 94 * mm, "Нашли вещь?")
    pdf.drawCentredString(page_width / 2, bottom + 87 * mm, "Отсканируйте код")
    pdf.drawImage(
        ImageReader(BytesIO(png_label(url))),
        left + 15 * mm,
        bottom + 23 * mm,
        60 * mm,
        60 * mm,
        mask="auto",
    )
    font_size = 10
    while True:
        style = ParagraphStyle(
            "Item", fontName=font, fontSize=font_size, leading=font_size + 2, alignment=1
        )
        paragraph = Paragraph(escape(name), style)
        _, text_height = paragraph.wrap(width - 8 * mm, 18 * mm)
        if text_height <= 18 * mm or font_size <= 7:
            break
        font_size -= 0.5
    paragraph.drawOn(pdf, left + 4 * mm, bottom + 20 * mm - text_height)
    pdf.setFont(font, 10)
    pdf.drawCentredString(
        page_width / 2, 113 * mm, "Прикрепите метку к вещи и проверьте сканирование камерой."
    )
    pdf.drawCentredString(
        page_width / 2, 106 * mm, "Не обрезайте белые поля вокруг QR. Защитите метку от влаги."
    )
    pdf.drawCentredString(
        page_width / 2, 99 * mm, "Связь через страницу работает при действующем доступе."
    )
    pdf.save()
    return output.getvalue()
