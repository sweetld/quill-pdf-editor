"""Shared helper for inserting text that always renders.

PyMuPDF's ``insert_textbox`` writes *nothing* and returns a negative value when
the text does not fit the rectangle. That is the cause of "replaced text just
disappears": a slightly-too-long string silently vanishes. This helper shrinks
the font until the text fits, and as a last resort grows the box downward.
"""
from __future__ import annotations

import fitz

MIN_FONTSIZE = 4.0


def insert_textbox_fit(page: "fitz.Page", rect: "fitz.Rect", text: str,
                       fontsize: float, color, fontname: str = "helv",
                       align: int = fitz.TEXT_ALIGN_LEFT) -> float:
    """Insert ``text`` into ``rect``, shrinking to fit. Returns the size used.

    A failed insert_textbox writes nothing, so retrying at smaller sizes never
    stacks partial text.
    """
    if not text:
        return fontsize
    size = max(MIN_FONTSIZE, float(fontsize))
    rc = page.insert_textbox(rect, text, fontsize=size, fontname=fontname,
                             color=color, align=align)
    while rc < 0 and size > MIN_FONTSIZE:
        size = max(MIN_FONTSIZE, size * 0.9)
        rc = page.insert_textbox(rect, text, fontsize=size, fontname=fontname,
                                 color=color, align=align)
    if rc < 0:
        # still overflowing at the minimum size: extend the box to the page foot
        grown = fitz.Rect(rect.x0, rect.y0, rect.x1, max(rect.y1, page.rect.y1 - 4))
        page.insert_textbox(grown, text, fontsize=MIN_FONTSIZE, fontname=fontname,
                            color=color, align=align)
        size = MIN_FONTSIZE
    return size
