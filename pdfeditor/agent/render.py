"""Page rendering for agents: a PNG of a page (or a region of it), optionally
overlaid with labelled element boxes and a coordinate grid so a vision model
can map what it sees onto element ids and PDF coordinates."""
from __future__ import annotations

import io
from typing import Dict, Optional, Tuple

import fitz
from PIL import Image, ImageDraw, ImageFont

from .engine import Layout, rect_list

_KIND_COLORS = {
    "text": (30, 100, 230), "image": (20, 160, 60), "drawing": (235, 130, 0),
    "field": (150, 40, 200), "annotation": (220, 30, 30),
}
MAX_LABELLED_DRAWINGS = 120
GRID_STEPS = (5, 10, 20, 25, 50, 100, 200)


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def render_page(page: fitz.Page, layout: Optional[Layout] = None,
                region: Optional[fitz.Rect] = None, dpi: Optional[float] = None,
                grid: bool = False, max_side: int = 1400) -> Tuple[bytes, Dict]:
    """Render ``page`` to PNG. ``region`` and all overlay geometry are in the
    page's unrotated coordinate space (the space layouts and edits use)."""
    rot = page.rotation_matrix
    clip = fitz.Rect(region) * rot if region is not None else fitz.Rect(page.rect)
    clip &= page.rect
    if clip.is_empty:
        raise ValueError("region is outside the page")
    if dpi:
        zoom = float(dpi) / 72.0
    else:
        zoom = max_side / max(clip.width, clip.height)
    zoom = max(0.25, min(zoom, 8.0))
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, annots=True, alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

    def to_px(x: float, y: float) -> Tuple[float, float]:
        p = fitz.Point(x, y) * rot
        return (p.x - clip.x0) * zoom, (p.y - clip.y0) * zoom

    def rect_px(r: fitz.Rect):
        a, b = to_px(r.x0, r.y0), to_px(r.x1, r.y1)
        return (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))

    if grid:
        _draw_grid(img, page, region, zoom, to_px)
    labelled = 0
    if layout is not None:
        labelled = _draw_overlay(img, layout, region, rect_px)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    meta = {"page": page.number + 1, "image_px": [img.width, img.height],
            "zoom": round(zoom, 3), "pt_per_px": round(1 / zoom, 3),
            "region": rect_list(region) if region is not None else None}
    if page.rotation:
        meta["rotation"] = page.rotation
    if layout is not None:
        meta["labelled_elements"] = labelled
    return buf.getvalue(), meta


def _draw_overlay(img: Image.Image, lay: Layout, region, rect_px) -> int:
    over = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    font = _font(max(11, min(16, img.width // 90)))
    drawings = [e for e in lay.of_kind("drawing") if not e.info.get("background")]
    drawings.sort(key=lambda e: -abs(e.rect))
    texts = []
    for e in lay.of_kind("text"):
        # rows of a table come out as one block with side-by-side lines:
        # label those lines individually so each cell is addressable
        if len(e.lines) > 1 and _side_by_side(e):
            texts.extend(lay.elements[tl.id] for tl in e.lines)
        else:
            texts.append(e)
    elements = (texts + lay.of_kind("image")
                + drawings[:MAX_LABELLED_DRAWINGS] + lay.of_kind("field")
                + lay.of_kind("annotation"))
    count = 0
    taken = []
    for e in elements:
        if region is not None and not e.rect.intersects(region):
            continue
        color = _KIND_COLORS["text" if e.kind == "line" else e.kind]
        box = rect_px(e.rect)
        if box[2] - box[0] < 2:
            box = (box[0] - 1, box[1], box[2] + 1, box[3])
        if box[3] - box[1] < 2:
            box = (box[0], box[1] - 1, box[2], box[3] + 1)
        d.rectangle(box, outline=color + (230,), width=2,
                    fill=color + (22,) if e.kind != "drawing" else None)
        tw = d.textlength(e.id, font=font)
        th = font.size if hasattr(font, "size") else 11
        lx, ly = box[0], box[1] - th - 3
        if ly < 0:
            ly = box[1] + 1
        # nudge labels that would sit on top of an earlier one
        for _ in range(6):
            if any(abs(lx - tx) < tw + 2 and abs(ly - ty) < th + 2 for tx, ty in taken):
                lx += tw + 4
            else:
                break
        taken.append((lx, ly))
        d.rectangle((lx, ly, lx + tw + 4, ly + th + 3), fill=color + (235,))
        d.text((lx + 2, ly + 1), e.id, fill=(255, 255, 255, 255), font=font)
        count += 1
    img.paste(Image.alpha_composite(img.convert("RGBA"), over).convert("RGB"))
    return count


def _side_by_side(block) -> bool:
    lines = block.lines
    return any(a.rect.x1 < b.rect.x0 or b.rect.x1 < a.rect.x0
               for i, a in enumerate(lines) for b in lines[i + 1:]
               if a.rect.y0 < b.rect.y1 and b.rect.y0 < a.rect.y1)


def _draw_grid(img: Image.Image, page: fitz.Page, region, zoom: float, to_px) -> None:
    area = fitz.Rect(region) if region is not None else page.mediabox
    span = max(area.width, area.height)
    step = next((s for s in GRID_STEPS if span / s <= 16), GRID_STEPS[-1])
    over = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    font = _font(11)
    prefix = bool(page.rotation)  # axes are swapped/flipped on screen: say which

    def gridline(a, b, label):
        d.line([a, b], fill=(255, 0, 120, 70), width=1)
        if abs(a[0] - b[0]) < 1:   # vertical on screen: label at the top
            pos = (a[0] + 2, min(a[1], b[1]) + 2)
        else:                      # horizontal on screen: label at the left
            pos = (min(a[0], b[0]) + 2, a[1] + 1)
        d.text(pos, label, fill=(200, 0, 90, 255), font=font)

    x = (int(area.x0) // step) * step
    while x <= area.x1:
        if x >= area.x0:
            gridline(to_px(x, area.y0), to_px(x, area.y1), f"x={x}" if prefix else str(x))
        x += step
    y = (int(area.y0) // step) * step
    while y <= area.y1:
        if y >= area.y0:
            gridline(to_px(area.x0, y), to_px(area.x1, y), f"y={y}" if prefix else str(y))
        y += step
    img.paste(Image.alpha_composite(img.convert("RGBA"), over).convert("RGB"))
