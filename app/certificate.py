"""Certificate rendering: a single predefined landscape-A4 PDF template (ReportLab)."""
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

GOLD = HexColor("#B8860B")
NAVY = HexColor("#1F2A44")
GREY = HexColor("#555555")

# Prefer a Unicode TTF so non-Latin names render; fall back to built-in fonts.
_FONT_CANDIDATES = [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"),
    ("/Library/Fonts/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf"),
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
]


class CertificateError(Exception):
    """Raised when a certificate cannot be rendered for a specific recipient."""


def _register_fonts() -> tuple[str, str, bool]:
    """Returns (regular, bold, is_unicode)."""
    for regular, bold in _FONT_CANDIDATES:
        if os.path.exists(regular) and os.path.exists(bold):
            try:
                pdfmetrics.registerFont(TTFont("CertFont", regular))
                pdfmetrics.registerFont(TTFont("CertFont-Bold", bold))
                return "CertFont", "CertFont-Bold", True
            except Exception:  # corrupt/unsupported font -> try the next one
                continue
    return "Times-Roman", "Times-Bold", False


FONT, FONT_BOLD, UNICODE_FONT = _register_fonts()


@dataclass
class CertificateData:
    recipient_name: str
    event_name: str
    issued_by: str
    issue_date: date
    achievement: str | None = None
    certificate_id: str = ""


def _fit_font_size(text: str, font: str, max_width: float, start: int, minimum: int = 14) -> int:
    size = start
    while size > minimum and pdfmetrics.stringWidth(text, font, size) > max_width:
        size -= 1
    return size


def _check_renderable(data: CertificateData) -> None:
    if UNICODE_FONT:
        return
    for label, value in (("name", data.recipient_name), ("achievement", data.achievement or "")):
        try:
            value.encode("cp1252")  # what the built-in PDF fonts can display
        except UnicodeEncodeError:
            raise CertificateError(f"recipient {label} contains characters the certificate font cannot render")


def render_certificate(data: CertificateData, output_path: Path) -> None:
    """Render to a temp file then rename, so a failure never leaves a half-written PDF."""
    _check_renderable(data)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(".pdf.tmp")
    width, height = landscape(A4)
    cx = width / 2
    max_text_width = width - 160

    try:
        c = canvas.Canvas(str(tmp_path), pagesize=(width, height))
        c.setTitle(f"Certificate - {data.recipient_name}")
        c.setAuthor(data.issued_by)

        # Double border
        c.setStrokeColor(GOLD)
        c.setLineWidth(4)
        c.rect(25, 25, width - 50, height - 50)
        c.setLineWidth(1)
        c.rect(35, 35, width - 70, height - 70)

        c.setFillColor(NAVY)
        c.setFont(FONT_BOLD, 40)
        c.drawCentredString(cx, height - 120, "CERTIFICATE")
        c.setFont(FONT, 16)
        c.setFillColor(GREY)
        c.drawCentredString(cx, height - 150, "OF ACHIEVEMENT" if data.achievement else "OF PARTICIPATION")

        c.setFont(FONT, 14)
        c.drawCentredString(cx, height - 210, "This is to certify that")

        c.setFillColor(NAVY)
        size = _fit_font_size(data.recipient_name, FONT_BOLD, max_text_width, 38)
        c.setFont(FONT_BOLD, size)
        c.drawCentredString(cx, height - 262, data.recipient_name)
        c.setStrokeColor(GOLD)
        c.setLineWidth(1)
        c.line(cx - 200, height - 272, cx + 200, height - 272)

        c.setFillColor(GREY)
        c.setFont(FONT, 14)
        c.drawCentredString(cx, height - 305, "has successfully participated in")

        c.setFillColor(NAVY)
        size = _fit_font_size(data.event_name, FONT_BOLD, max_text_width, 26)
        c.setFont(FONT_BOLD, size)
        c.drawCentredString(cx, height - 345, data.event_name)

        if data.achievement:
            c.setFillColor(GREY)
            size = _fit_font_size(data.achievement, FONT, max_text_width, 16, 10)
            c.setFont(FONT, size)
            c.drawCentredString(cx, height - 375, data.achievement)

        # Footer: date (left), issuer (right)
        c.setFillColor(GREY)
        c.setFont(FONT, 12)
        c.drawString(80, 90, f"Date: {data.issue_date.strftime('%d %B %Y')}")
        c.drawRightString(width - 80, 90, f"Issued by: {data.issued_by}")
        c.setFont(FONT, 8)
        if data.certificate_id:
            c.drawCentredString(cx, 55, f"Certificate ID: {data.certificate_id}")
        c.showPage()
        c.save()
        os.replace(tmp_path, output_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
