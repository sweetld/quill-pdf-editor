"""Builds a realistic test PDF: subset-embedded fonts, styled text, a coloured
panel, an image, a ruled table, a form page with a background, and a rotated
page."""
from __future__ import annotations

import io
import os

import fitz
from PIL import Image

SERIF_TTF = next((p for p in (
    "/usr/share/fonts/truetype/liberation2/LiberationSerif-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf") if os.path.exists(p)), None)

PARAGRAPH = ("Revenue grew steadily across all regions this quarter. The team "
             "expanded into two new markets and completed the platform migration "
             "ahead of schedule.")


def _png(w=120, h=80, color=(40, 120, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def build_fixture(path: str) -> str:
    doc = fitz.open()
    p = doc.new_page(width=595, height=842)
    if SERIF_TTF:
        p.insert_font(fontname="LibSerif", fontfile=SERIF_TTF)
        serif = "LibSerif"
    else:
        serif = "tiro"
    p.insert_text((72, 90), "Quarterly Report", fontname=serif, fontsize=22,
                  color=(0.1, 0.2, 0.5))
    p.insert_textbox(fitz.Rect(72, 105, 520, 170), PARAGRAPH, fontname=serif, fontsize=11)
    p.insert_text((72, 200), "DRAFT - internal only", fontname="hebo", fontsize=12,
                  color=(0.8, 0, 0))
    # coloured panel with contact text
    p.draw_rect(fitz.Rect(72, 230, 300, 330), color=(0.6, 0.5, 0), fill=(1, 1, 0.8), width=1)
    p.insert_text((84, 255), "Contact: Alice", fontname="helv", fontsize=12)
    p.insert_text((84, 275), "Phone: 555-0100", fontname="helv", fontsize=12)
    p.insert_image(fitz.Rect(350, 230, 500, 330), stream=_png())
    # ruled table
    top, left, right = 360, 72, 520
    for i in range(4):
        y = top + i * 30
        p.draw_line((left, y), (right, y), color=(0, 0, 0), width=0.8)
    for x in (left, 296, right):
        p.draw_line((x, top), (x, top + 90), color=(0, 0, 0), width=0.8)
    for i, (a, b) in enumerate([("Region", "Revenue"), ("North", "1,200"), ("South", "950")]):
        p.insert_text((left + 6, top + 20 + i * 30), a, fontname="helv", fontsize=11)
        p.insert_text((302, top + 20 + i * 30), b, fontname="helv", fontsize=11)
    p.insert_text((72, 800), "Page 1 footer", fontname="helv", fontsize=9,
                  color=(0.4, 0.4, 0.4))

    # page 2: form on a tinted background
    f = doc.new_page(width=595, height=842)
    f.draw_rect(f.rect, color=None, fill=(0.94, 0.95, 0.98))
    f.insert_text((72, 80), "Registration Form", fontname="hebo", fontsize=18)
    labels = [("Name:", 120), ("Email:", 160), ("Country:", 200), ("Subscribe:", 240)]
    for text, y in labels:
        f.insert_text((72, y), text, fontname="helv", fontsize=11)
    for name, y in (("name", 120), ("email", 160)):
        w = fitz.Widget()
        w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
        w.field_name = name
        w.rect = fitz.Rect(160, y - 14, 400, y + 4)
        w.text_fontsize = 11
        f.add_widget(w)
    w = fitz.Widget()
    w.field_type = fitz.PDF_WIDGET_TYPE_COMBOBOX
    w.field_name = "country"
    w.choice_values = ["UK", "France", "Germany"]
    w.rect = fitz.Rect(160, 186, 300, 204)
    w.field_value = "UK"
    f.add_widget(w)
    w = fitz.Widget()
    w.field_type = fitz.PDF_WIDGET_TYPE_CHECKBOX
    w.field_name = "subscribe"
    w.rect = fitz.Rect(160, 228, 174, 242)
    w.field_value = False
    f.add_widget(w)

    # page 3: rotated
    r = doc.new_page(width=595, height=842)
    r.insert_text((72, 100), "Landscape appendix", fontname="helv", fontsize=14)
    r.set_rotation(90)

    doc.subset_fonts()
    doc.save(path, garbage=4, deflate=True)
    doc.close()
    return path
