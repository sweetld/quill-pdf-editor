"""Headless editing engine for AI agents (no Qt).

``AgentDocument`` wraps one ``fitz.Document`` and exposes the operations an
agent needs to inspect and rewrite a PDF: a structured page layout with stable
element ids, style-preserving text edits, true content deletion (redaction
without white boxes), moving/scaling regions as vector content, form fields,
page management, and snapshot-based undo/redo.

Conventions (shared with the MCP server):
  * Page numbers are 1-based.
  * Coordinates are PDF points in the page's *unrotated* space, origin at the
    top-left, y growing downward (PyMuPDF's convention). ``rect`` is
    ``[x0, y0, x1, y1]``.
  * Element ids (``t3``, ``t3.1``, ``i0``, ``g5``, ``w2``, ``a1``) come from
    ``layout()`` and are only valid until that page is modified. Using an id
    from an outdated layout raises ``EngineError`` instead of editing the
    wrong thing.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import fitz

from ..fonts import base14_alias, style_from_flags
from ..textfit import insert_textbox_fit

# Text extraction without image blocks (those would drag image bytes along).
TEXT_FLAGS = (fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_PRESERVE_LIGATURES
              | fitz.TEXT_MEDIABOX_CLIP)
UNDO_LIMIT = 40
MAX_DRAWINGS_LISTED = 200
_ALIGN = {"left": fitz.TEXT_ALIGN_LEFT, "center": fitz.TEXT_ALIGN_CENTER,
          "right": fitz.TEXT_ALIGN_RIGHT, "justify": fitz.TEXT_ALIGN_JUSTIFY}
_NAMED_COLORS = {
    "black": (0, 0, 0), "white": (1, 1, 1), "red": (1, 0, 0), "green": (0, 0.5, 0),
    "blue": (0, 0, 1), "yellow": (1, 1, 0), "gray": (0.5, 0.5, 0.5),
    "grey": (0.5, 0.5, 0.5), "orange": (1, 0.55, 0), "purple": (0.5, 0, 0.5),
}
_FIELD_TYPES = {
    "text": fitz.PDF_WIDGET_TYPE_TEXT, "checkbox": fitz.PDF_WIDGET_TYPE_CHECKBOX,
    "radio": fitz.PDF_WIDGET_TYPE_RADIOBUTTON, "combobox": fitz.PDF_WIDGET_TYPE_COMBOBOX,
    "listbox": fitz.PDF_WIDGET_TYPE_LISTBOX, "button": fitz.PDF_WIDGET_TYPE_BUTTON,
    "signature": fitz.PDF_WIDGET_TYPE_SIGNATURE,
}


class EngineError(Exception):
    """A user-facing error: bad arguments, stale ids, unsupported content."""


# ---- small conversions ---------------------------------------------------
def rect_list(r) -> List[float]:
    r = fitz.Rect(r)
    return [round(r.x0, 1), round(r.y0, 1), round(r.x1, 1), round(r.y1, 1)]


def to_rect(value, what: str = "rect") -> fitz.Rect:
    try:
        r = fitz.Rect(*value) if len(value) == 4 else None
    except Exception:  # noqa: BLE001
        r = None
    if r is None or r.is_infinite:
        raise EngineError(f"{what} must be [x0, y0, x1, y1] in PDF points")
    r.normalize()
    return r


def hex_color(color) -> Optional[str]:
    """A span colour int or a 0..1 float tuple as #rrggbb."""
    if color is None:
        return None
    if isinstance(color, int):
        return f"#{color & 0xFFFFFF:06x}"
    if len(color) == 1:
        color = (color[0],) * 3
    if len(color) == 4:  # CMYK -> RGB, approximate
        c, m, y, k = color
        color = ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
    return "#" + "".join(f"{max(0, min(255, round(v * 255))):02x}" for v in color[:3])


def parse_color(value, default=(0, 0, 0)) -> Optional[Tuple[float, float, float]]:
    """'#rrggbb', a name, [r,g,b] in 0..1 or 0..255, or None/'none'."""
    if value is None:
        return default
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("", "none", "transparent"):
            return None
        if v in _NAMED_COLORS:
            return _NAMED_COLORS[v]
        v = v.lstrip("#")
        if len(v) == 3:
            v = "".join(ch * 2 for ch in v)
        if len(v) == 6:
            try:
                return tuple(int(v[i:i + 2], 16) / 255 for i in (0, 2, 4))
            except ValueError:
                pass
        raise EngineError(f"Unrecognised colour {value!r}; use '#rrggbb'")
    vals = [float(x) for x in value]
    if len(vals) != 3:
        raise EngineError("Colour lists must have 3 components")
    if any(x > 1 for x in vals):
        vals = [x / 255 for x in vals]
    return tuple(vals)


def _int_color_to_rgb(color: int) -> Tuple[float, float, float]:
    return fitz.sRGB_to_pdf(color)


# ---- layout model --------------------------------------------------------
@dataclass
class TextLine:
    id: str
    rect: fitz.Rect
    dir: Tuple[float, float]
    spans: List[dict]
    text: str


@dataclass
class Element:
    id: str
    kind: str                      # text | line | image | drawing | field | annotation
    rect: fitz.Rect
    info: Dict = field(default_factory=dict)
    lines: List[TextLine] = field(default_factory=list)
    xref: int = 0


@dataclass
class Layout:
    page: int                      # 1-based
    width: float
    height: float
    rotation: int
    elements: Dict[str, Element] = field(default_factory=dict)

    def of_kind(self, *kinds: str) -> List[Element]:
        return [e for e in self.elements.values() if e.kind in kinds]


def span_style(span: dict) -> Dict:
    st = style_from_flags(span.get("flags", 0), span.get("font", ""))
    return {"font": span.get("font", ""), "size": round(span.get("size", 0), 2),
            "color": hex_color(span.get("color", 0)), "bold": st["bold"],
            "italic": st["italic"], "family": st["family"]}


def _dominant_span(spans: Sequence[dict]) -> dict:
    return max(spans, key=lambda s: len(s.get("text", "").strip()))


def _drawing_shape(d: dict) -> str:
    kinds = {it[0] for it in d.get("items", [])}
    if kinds == {"re"}:
        return "rect" if len(d["items"]) == 1 else "rects"
    if kinds == {"l"}:
        return "line" if len(d["items"]) == 1 else "polyline"
    if kinds == {"qu"}:
        return "quad"
    return "path"


def build_layout(page: fitz.Page) -> Layout:
    lay = Layout(page.number + 1, round(page.rect.width, 1), round(page.rect.height, 1),
                 page.rotation)
    els = lay.elements
    data = page.get_text("dict", flags=TEXT_FLAGS)
    blocks = [b for b in data.get("blocks", []) if b.get("type", 0) == 0]
    # Reading order rather than content-stream order: re-inserted text then
    # keeps its id position instead of jumping to the end.
    blocks.sort(key=lambda b: (round(b["bbox"][1] / 3), b["bbox"][0]))
    tn = 0
    for block in blocks:
        bid = f"t{tn}"
        lines: List[TextLine] = []
        for ln in block.get("lines", []):
            spans = [s for s in ln.get("spans", []) if s.get("text")]
            text = "".join(s["text"] for s in spans)
            if not text.strip():
                continue
            lines.append(TextLine(f"{bid}.{len(lines)}", fitz.Rect(ln["bbox"]),
                                  tuple(ln.get("dir", (1, 0))), spans, text))
        if not lines:
            continue
        tn += 1
        rect = _union(tl.rect for tl in lines)
        all_spans = [s for tl in lines for s in tl.spans]
        els[bid] = Element(bid, "text", rect, {"style": span_style(_dominant_span(all_spans))},
                           lines=lines)
        for tl in lines:
            els[tl.id] = Element(tl.id, "line", tl.rect,
                                 {"style": span_style(_dominant_span(tl.spans)),
                                  "block": bid}, lines=[tl])

    images = [i for i in page.get_image_info(xrefs=True)
              if fitz.Rect(i["bbox"]).is_valid and not fitz.Rect(i["bbox"]).is_empty]
    for n, info in enumerate(images):
        eid = f"i{n}"
        els[eid] = Element(eid, "image", fitz.Rect(info["bbox"]),
                           {"px": [info.get("width"), info.get("height")]},
                           xref=info.get("xref", 0))

    page_area = abs(page.rect)
    for n, d in enumerate(page.get_drawings()):
        r = fitz.Rect(d["rect"])
        eid = f"g{n}"
        info = {"shape": _drawing_shape(d), "fill": hex_color(d.get("fill")),
                "stroke": hex_color(d.get("color")) if d.get("color") else None}
        if d.get("color") and d.get("width"):
            info["width"] = round(d["width"], 2)
        if page_area and abs(r) > 0.8 * page_area:
            info["background"] = True
        els[eid] = Element(eid, "drawing", r, info)

    for n, w in enumerate(page.widgets()):
        eid = f"w{n}"
        els[eid] = Element(eid, "field", fitz.Rect(w.rect), widget_info(w), xref=w.xref)

    for n, a in enumerate(page.annots()):
        eid = f"a{n}"
        els[eid] = Element(eid, "annotation", fitz.Rect(a.rect),
                           {"type": a.type[1], "content": a.info.get("content", "")},
                           xref=a.xref)
    return lay


def widget_info(w: fitz.Widget) -> Dict:
    info = {"name": w.field_name, "type": w.field_type_string.lower(),
            "value": w.field_value}
    if w.field_type in (fitz.PDF_WIDGET_TYPE_COMBOBOX, fitz.PDF_WIDGET_TYPE_LISTBOX):
        info["options"] = w.choice_values
    if w.field_type in (fitz.PDF_WIDGET_TYPE_CHECKBOX, fitz.PDF_WIDGET_TYPE_RADIOBUTTON):
        info["on_state"] = w.on_state()
    if w.field_label:
        info["label"] = w.field_label
    if w.field_flags & fitz.PDF_FIELD_IS_READ_ONLY:
        info["read_only"] = True
    if w.field_flags & fitz.PDF_FIELD_IS_REQUIRED:
        info["required"] = True
    if w.field_type == fitz.PDF_WIDGET_TYPE_TEXT and w.text_fontsize:
        info["font_size"] = w.text_fontsize
    return info


def layout_to_dict(lay: Layout, detail: str = "lines", kinds: Optional[Iterable[str]] = None,
                   region: Optional[fitz.Rect] = None) -> Dict:
    """JSON-ready view of a layout. ``kinds`` filters element categories
    (text, images, drawings, fields, annotations); ``region`` keeps only
    elements intersecting it."""
    want = set(kinds or ("text", "images", "drawings", "fields", "annotations"))
    out: Dict = {"page": lay.page, "width": lay.width, "height": lay.height}
    if lay.rotation:
        out["rotation"] = lay.rotation

    def keep(e: Element) -> bool:
        return region is None or e.rect.intersects(region) or (
            e.rect.is_empty and region.contains(e.rect.tl))

    if "text" in want:
        texts = []
        for e in lay.of_kind("text"):
            if not keep(e):
                continue
            item = {"id": e.id, "bbox": rect_list(e.rect)}
            item["text"] = "\n".join(tl.text for tl in e.lines)
            item.update(e.info["style"])
            if detail != "blocks" and len(e.lines) > 1:
                item["lines"] = [_line_dict(tl, e.info["style"], detail) for tl in e.lines]
            elif detail == "spans":
                spans = _spans_dict(e.lines[0])
                if len(spans) > 1:
                    item["spans"] = spans
            texts.append(item)
        out["text"] = texts
    if "images" in want:
        out["images"] = [{"id": e.id, "bbox": rect_list(e.rect), **e.info}
                         for e in lay.of_kind("image") if keep(e)]
    if "drawings" in want:
        drawings = [e for e in lay.of_kind("drawing") if keep(e)]
        listed = drawings[:MAX_DRAWINGS_LISTED]
        out["drawings"] = [{"id": e.id, "bbox": rect_list(e.rect),
                            **{k: v for k, v in e.info.items() if v is not None}}
                           for e in listed]
        if len(drawings) > len(listed):
            out["drawings_omitted"] = len(drawings) - len(listed)
    if "fields" in want:
        out["fields"] = [{"id": e.id, "bbox": rect_list(e.rect), **e.info}
                         for e in lay.of_kind("field") if keep(e)]
    if "annotations" in want:
        out["annotations"] = [{"id": e.id, "bbox": rect_list(e.rect), **e.info}
                              for e in lay.of_kind("annotation") if keep(e)]
    return out


def _line_dict(tl: TextLine, block_style: Dict, detail: str) -> Dict:
    d = {"id": tl.id, "bbox": rect_list(tl.rect), "text": tl.text}
    st = span_style(_dominant_span(tl.spans))
    for key in ("font", "size", "color", "bold", "italic"):
        if st[key] != block_style.get(key):
            d[key] = st[key]
    if detail == "spans":
        spans = _spans_dict(tl)
        if len(spans) > 1:
            d["spans"] = spans
    return d


def _spans_dict(tl: TextLine) -> List[Dict]:
    out = []
    for s in tl.spans:
        st = span_style(s)
        out.append({"text": s["text"], "font": st["font"], "size": st["size"],
                    "color": st["color"], "bold": st["bold"], "italic": st["italic"]})
    return out


# ---- low-level page edits -------------------------------------------------
def _text_band(tl: TextLine) -> fitz.Rect:
    """A rect strictly inside a line's glyph boxes, so redacting it removes the
    line's characters without clipping neighbouring lines that touch it."""
    r = fitz.Rect(tl.rect)
    if abs(tl.dir[0]) >= abs(tl.dir[1]):
        dy = r.height * 0.3
        return fitz.Rect(r.x0 + 0.3, r.y0 + dy, r.x1 - 0.3, r.y1 - dy)
    dx = r.width * 0.3
    return fitz.Rect(r.x0 + dx, r.y0 + 0.3, r.x1 - dx, r.y1 - 0.3)


def _apply(page: fitz.Page, rects: Iterable[fitz.Rect], *, text: bool = False,
           images: int = fitz.PDF_REDACT_IMAGE_NONE,
           graphics: int = fitz.PDF_REDACT_LINE_ART_NONE) -> None:
    """Redact ``rects`` with no fill colour (true removal, no white boxes)."""
    rects = [fitz.Rect(r) for r in rects]
    if not rects:
        return
    if any(a.type[0] == fitz.PDF_ANNOT_REDACT for a in page.annots()):
        # apply_redactions would also burn in these pre-existing marks
        raise EngineError(f"Page {page.number + 1} has pending redaction annotations from "
                          "another tool; delete them first (they're listed as annotations "
                          "in get_page_layout)")
    for r in rects:
        if r.is_empty:  # zero-height/width lines still need an area
            r = fitz.Rect(r.x0 - 0.5, r.y0 - 0.5, r.x1 + 0.5, r.y1 + 0.5)
        page.add_redact_annot(r, fill=False)
    page.apply_redactions(images=images, graphics=graphics,
                          text=fitz.PDF_REDACT_TEXT_REMOVE if text else fitz.PDF_REDACT_TEXT_NONE)


def remove_text_lines(page: fitz.Page, lines: Iterable[TextLine]) -> None:
    _apply(page, [_text_band(tl) for tl in lines], text=True)


def remove_image(page: fitz.Page, el: Element, layout: Layout) -> str:
    """Remove one placed image. Uses a removal redaction when no other image
    overlaps it; otherwise replaces that image's xref if it's used once."""
    others = [e for e in layout.of_kind("image")
              if e.id != el.id and e.rect.intersects(el.rect)]
    if not others:
        _apply(page, [el.rect + (1, 1, -1, -1) if el.rect.width > 4 and el.rect.height > 4
                      else el.rect], images=fitz.PDF_REDACT_IMAGE_REMOVE)
        return "removed"
    if not el.xref:
        raise EngineError(f"{el.id} overlaps other images and is inline; use delete_region "
                          "with images=true to blank that area instead")
    uses = sum(1 for p in page.parent for i in p.get_image_info(xrefs=True)
               if i.get("xref") == el.xref)
    if uses > 1:
        raise EngineError(f"{el.id} overlaps another image and its picture is reused "
                          f"{uses} times in the document; use delete_region instead")
    page.delete_image(el.xref)
    return "removed (image data cleared)"


def remove_drawing(page: fitz.Page, el: Element) -> None:
    r = fitz.Rect(el.rect.x0 - 1, el.rect.y0 - 1, el.rect.x1 + 1, el.rect.y1 + 1)
    _apply(page, [r], graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED)


def _find_widget(page: fitz.Page, xref: int) -> Optional[fitz.Widget]:
    for w in page.widgets():
        if w.xref == xref:
            return w
    return None


# ---- fonts ----------------------------------------------------------------
# Metric-compatible open substitutes for common proprietary fonts.
_FONT_ALIASES = {
    "arial": "liberationsans", "timesnewroman": "liberationserif",
    "couriernew": "liberationmono",
    "calibri": "carlito", "cambria": "caladea",
}
_BASE14_RE = re.compile(r"^(helvetica(bold)?(italic)?|times(roman|bold|italic|bolditalic)"
                        r"|courier(bold)?(italic)?|symbol|zapfdingbats)$")
_SYSTEM_FONTS: Optional[Dict[str, str]] = None
_FONT_CACHE: Dict[str, fitz.Font] = {}


def _norm_font(name: str) -> str:
    name = name.split("+", 1)[-1].lower()
    name = "".join(ch for ch in name if ch.isalnum())
    for suffix in ("psmt", "mt", "ps"):
        if name.endswith(suffix) and len(name) > len(suffix) + 3:
            name = name[: -len(suffix)]
            break
    return name.replace("oblique", "italic")


def _system_fonts() -> Dict[str, str]:
    """normalised font name -> font file, from fontconfig (empty if unavailable)."""
    global _SYSTEM_FONTS
    if _SYSTEM_FONTS is not None:
        return _SYSTEM_FONTS
    index: Dict[str, str] = {}
    try:
        import subprocess
        out = subprocess.run(["fc-list", "--format", "%{file}|%{fullname}|%{postscriptname}\\n"],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception:  # noqa: BLE001 - no fontconfig
        out = ""
    for row in out.splitlines():
        parts = row.split("|")
        if len(parts) != 3 or not parts[0].lower().endswith((".ttf", ".otf")):
            continue
        path, full, ps = parts
        for name in {full.split(",")[0], ps}:
            key = _norm_font(name)
            if key:
                index.setdefault(key, path)
                index.setdefault(key + "regular", path)
    _SYSTEM_FONTS = index
    return index


def system_font_file(span_font: str) -> Optional[str]:
    """An installed font file for a PDF font name, or a metric-compatible
    substitute (e.g. Arial -> Liberation Sans)."""
    key = _norm_font(span_font)
    if not key or _BASE14_RE.match(key):
        return None  # standard PDF fonts: the Base-14 fallback is the exact match
    fonts = _system_fonts()
    if key in fonts:
        return fonts[key]
    for prefix, target in _FONT_ALIASES.items():
        if key.startswith(prefix):
            alt = target + key[len(prefix):]
            if alt in fonts:
                return fonts[alt]
    return None


def _font_from_file(path: str) -> fitz.Font:
    if path not in _FONT_CACHE:
        _FONT_CACHE[path] = fitz.Font(fontfile=path)
    return _FONT_CACHE[path]


class FontResolver:
    """Picks a font for re-inserted text, best match first:
    1. the original embedded font, if it maps every needed character;
    2. the same font installed on this system (or a metric-compatible
       substitute such as Liberation Sans for Arial);
    3. a Base-14 font of the same family/weight/slant (always renders)."""

    def __init__(self, page: fitz.Page) -> None:
        self.page = page
        self._by_name: Dict[str, Optional[Tuple[str, fitz.Font, bytes]]] = {}
        self._fonts = page.get_fonts(full=True)

    def _embedded(self, span_font: str) -> Optional[Tuple[str, fitz.Font, bytes]]:
        if span_font in self._by_name:
            return self._by_name[span_font]
        found = None
        want = _norm_font(span_font)
        names = {want, want + "regular", want + "book", want + "roman"}
        for xref, ext, _typ, basefont, *_rest in self._fonts:
            base = _norm_font(basefont)
            if ext in ("n/a", "") or not (base in names or want in
                                           (base + "regular", base + "book", base + "roman")):
                continue
            try:
                _name, _ext, _t, buf = self.page.parent.extract_font(xref)
                if buf:
                    found = (f"QF{xref}", fitz.Font(fontbuffer=buf), buf)
                    break
            except Exception:  # noqa: BLE001 - unusable font program
                continue
        self._by_name[span_font] = found
        return found

    @staticmethod
    def _covers(font: fitz.Font, text: str) -> bool:
        return all(font.has_glyph(ord(ch)) for ch in text if not ch.isspace())

    def resolve(self, span: dict, text: str, override: Optional[Dict] = None
                ) -> Tuple[str, fitz.Font]:
        """(fontname registered on the page, fitz.Font for measuring)."""
        override = override or {}
        st = style_from_flags(span.get("flags", 0), span.get("font", ""))
        bold = override.get("bold", st["bold"])
        italic = override.get("italic", st["italic"])
        family = override.get("family") or st["family"]
        restyled = (override.get("bold") not in (None, st["bold"])
                    or override.get("italic") not in (None, st["italic"]))
        if family == "original" or (not override.get("family") and not restyled):
            span_font = span.get("font", "")
            emb = self._embedded(span_font)
            if emb is not None and self._covers(emb[1], text):
                self.page.insert_font(fontname=emb[0], fontbuffer=emb[2])
                return emb[0], emb[1]
            path = system_font_file(span_font)
            if path:
                font = _font_from_file(path)
                if self._covers(font, text):
                    alias = "QS" + _norm_font(os.path.basename(path))[:24]
                    self.page.insert_font(fontname=alias, fontfile=path)
                    return alias, font
            family = st["family"]
        alias = base14_alias(bold, italic, family == "serif", family == "mono")
        return alias, fitz.Font(alias)


def base14_for(family: str, bold: bool, italic: bool) -> str:
    family = (family or "sans").lower()
    if family in ("helv", "helvetica", "arial", "sans-serif"):
        family = "sans"
    elif family in ("times", "tiro", "times-roman"):
        family = "serif"
    elif family in ("courier", "cour", "monospace"):
        family = "mono"
    if family not in ("sans", "serif", "mono"):
        raise EngineError("font must be 'sans', 'serif' or 'mono'")
    return base14_alias(bold, italic, family == "serif", family == "mono")


# ---- the document session -------------------------------------------------
class AgentDocument:
    """One open PDF plus its undo history and per-page layout cache."""

    def __init__(self, path: str, password: Optional[str] = None) -> None:
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isfile(path):
            raise EngineError(f"No such file: {path}")
        with open(path, "rb") as fh:
            data = fh.read()
        doc = fitz.open("pdf", data)
        if doc.needs_pass and not (password and doc.authenticate(password)):
            doc.close()
            raise EngineError("Document is password protected; pass password=")
        self._password = password
        self.doc = doc
        self.path = path
        self.dirty = False
        self._undo: List[Tuple[str, bytes]] = []
        self._redo: List[Tuple[str, bytes]] = []
        self._page_rev: List[int] = [0] * doc.page_count
        self._layouts: Dict[int, Tuple[int, Layout]] = {}
        self.on_change: Optional[Callable[["AgentDocument", Optional[int]], None]] = None

    # ---- bookkeeping ---------------------------------------------------
    def page_index(self, page: int) -> int:
        n = self.doc.page_count
        if not isinstance(page, int) or not 1 <= page <= n:
            raise EngineError(f"page must be between 1 and {n} (got {page!r})")
        return page - 1

    def layout(self, page: int) -> Layout:
        idx = self.page_index(page)
        cached = self._layouts.get(idx)
        if cached and cached[0] == self._page_rev[idx]:
            return cached[1]
        lay = build_layout(self.doc[idx])
        self._layouts[idx] = (self._page_rev[idx], lay)
        return lay

    def resolve(self, page: int, ids: Sequence[str]) -> Tuple[Layout, List[Element]]:
        """Elements for ids taken from the *current* layout of ``page``."""
        idx = self.page_index(page)
        cached = self._layouts.get(idx)
        if not cached or cached[0] != self._page_rev[idx]:
            raise EngineError(
                f"Element ids for page {page} are out of date (the page changed, or its "
                "layout was never read). Call get_page_layout (or find_text) for this "
                "page again and use the new ids.")
        lay = cached[1]
        missing = [i for i in ids if i not in lay.elements]
        if missing:
            raise EngineError(f"Unknown element id(s) on page {page}: {', '.join(missing)}")
        if not ids:
            raise EngineError("No element ids given")
        return lay, [lay.elements[i] for i in ids]

    def _touch(self, indices: Iterable[int]) -> None:
        for i in indices:
            if 0 <= i < len(self._page_rev):
                self._page_rev[i] += 1

    def _reset_pages(self) -> None:
        # Bump every revision past any cached one so old ids can never match.
        top = max(self._page_rev, default=0) + 1
        self._page_rev = [top] * self.doc.page_count
        self._layouts.clear()

    def mutate(self, label: str, fn: Callable[[], object],
               pages: Optional[Iterable[int]] = None, structural: bool = False,
               focus: Optional[int] = None):
        """Run ``fn`` as one undoable step. ``pages`` are 0-based indices whose
        element ids become invalid; ``structural`` invalidates all pages."""
        snap = self.doc.tobytes()
        try:
            result = fn()
        except Exception:
            self._load_bytes(snap)
            self._reset_pages()
            raise
        self._undo.append((label, snap))
        del self._undo[:-UNDO_LIMIT]
        self._redo.clear()
        if structural:
            self._reset_pages()
        else:
            self._touch(pages or [])
        self.dirty = True
        if self.on_change:
            self.on_change(self, focus)
        return result

    def _load_bytes(self, data: bytes) -> None:
        self._page_refs = []
        self.doc.close()
        self.doc = fitz.open("pdf", data)
        if self.doc.needs_pass and self._password:
            self.doc.authenticate(self._password)

    def undo(self, steps: int = 1) -> List[str]:
        done = []
        for _ in range(max(1, steps)):
            if not self._undo:
                break
            label, snap = self._undo.pop()
            self._redo.append((label, self.doc.tobytes()))
            self._load_bytes(snap)
            done.append(label)
        if done:
            self._reset_pages()
            self.dirty = True
            if self.on_change:
                self.on_change(self, None)
        return done

    def redo(self, steps: int = 1) -> List[str]:
        done = []
        for _ in range(max(1, steps)):
            if not self._redo:
                break
            label, snap = self._redo.pop()
            self._undo.append((label, self.doc.tobytes()))
            self._load_bytes(snap)
            done.append(label)
        if done:
            self._reset_pages()
            self.dirty = True
            if self.on_change:
                self.on_change(self, None)
        return done

    def history(self) -> Dict:
        return {"undo": [l for l, _ in self._undo], "redo": [l for l, _ in reversed(self._redo)]}

    # ---- info / reading ------------------------------------------------
    def info(self) -> Dict:
        d = self.doc
        sizes = {}
        for p in d:
            key = f"{round(p.rect.width)}x{round(p.rect.height)}"
            sizes.setdefault(key, []).append(p.number + 1)
        fields = sum(1 for p in d for _ in p.widgets())
        return {"path": self.path, "pages": d.page_count, "unsaved_changes": self.dirty,
                "page_sizes_pt": {k: _compact_pages(v) for k, v in sizes.items()},
                "rotated_pages": [p.number + 1 for p in d if p.rotation],
                "form_fields": fields, "metadata": {k: v for k, v in d.metadata.items() if v},
                "outline": d.get_toc(simple=True)[:50], "history": self.history()}

    def page_text(self, pages: Optional[Sequence[int]] = None) -> Dict[int, str]:
        pages = pages or range(1, self.doc.page_count + 1)
        return {p: self.doc[self.page_index(p)].get_text("text", sort=True) for p in pages}

    def find_text(self, query: str, page: Optional[int] = None,
                  match_case: bool = False) -> List[Dict]:
        if not query:
            raise EngineError("query is empty")
        pages = [page] if page else range(1, self.doc.page_count + 1)
        hits = []
        for p in pages:
            pg = self.doc[self.page_index(p)]
            lay = self.layout(p)
            lines = [e for e in lay.of_kind("line")]
            for r in pg.search_for(query):
                line = next((e for e in lines if e.rect.contains((r.tl + r.br) / 2)), None)
                if match_case and line is not None and query not in line.lines[0].text:
                    continue
                hit = {"page": p, "bbox": rect_list(r)}
                if line is not None:
                    hit.update(line_id=line.id, block_id=line.info["block"],
                               line_text=line.lines[0].text)
                hits.append(hit)
        return hits

    # ---- deletion ------------------------------------------------------
    def delete_elements(self, page: int, ids: Sequence[str]) -> Dict:
        lay, els = self.resolve(page, ids)
        idx = self.page_index(page)
        notes: List[str] = []

        def run():
            pg = self.doc[idx]
            text_lines = [tl for e in els if e.kind in ("text", "line") for tl in e.lines]
            remove_text_lines(pg, _unique_lines(text_lines))
            for e in els:
                if e.kind == "image":
                    notes.append(f"{e.id}: {remove_image(pg, e, lay)}")
                elif e.kind == "drawing":
                    before = len(pg.get_drawings())
                    remove_drawing(pg, e)
                    removed = before - len(pg.get_drawings())
                    notes.append(f"{e.id}: {removed} vector path(s) removed")
                elif e.kind == "field":
                    w = _find_widget(pg, e.xref)
                    if w is not None:
                        pg.delete_widget(w)
                elif e.kind == "annotation":
                    a = pg.load_annot(e.xref)
                    if a is not None:
                        pg.delete_annot(a)
            return notes

        self.mutate(f"Delete {', '.join(ids)} on page {page}", run, [idx], focus=idx)
        return {"deleted": list(ids), "notes": notes}

    def delete_region(self, page: int, rect, text: bool = True, images: bool = True,
                      graphics: bool = True, fields: bool = False,
                      annotations: bool = False) -> Dict:
        idx = self.page_index(page)
        r = to_rect(rect)

        def run():
            pg = self.doc[idx]
            removed_fields = 0
            if fields or annotations:
                lay = build_layout(pg)
                for e in lay.elements.values():
                    if not r.contains((e.rect.tl + e.rect.br) / 2):
                        continue
                    if fields and e.kind == "field":
                        w = _find_widget(pg, e.xref)
                        if w is not None:
                            pg.delete_widget(w)
                            removed_fields += 1
                    elif annotations and e.kind == "annotation":
                        a = pg.load_annot(e.xref)
                        if a is not None:
                            pg.delete_annot(a)
            _apply(pg, [r], text=text,
                   images=fitz.PDF_REDACT_IMAGE_PIXELS if images else fitz.PDF_REDACT_IMAGE_NONE,
                   graphics=(fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED if graphics
                             else fitz.PDF_REDACT_LINE_ART_NONE))
            return removed_fields

        nf = self.mutate(f"Delete region on page {page}", run, [idx], focus=idx)
        res = {"page": page, "region": rect_list(r)}
        if nf:
            res["fields_removed"] = nf
        return res

    # ---- text ----------------------------------------------------------
    def edit_text(self, page: int, id: str, new_text: str, rect=None,
                  font_size: Optional[float] = None, color=None, font: Optional[str] = None,
                  bold: Optional[bool] = None, italic: Optional[bool] = None,
                  align: str = "left", line_height: Optional[float] = None) -> Dict:
        lay, (el,) = self.resolve(page, [id])
        if el.kind not in ("text", "line"):
            raise EngineError(f"{id} is not text (it is a {el.kind})")
        idx = self.page_index(page)
        if align not in _ALIGN:
            raise EngineError("align must be left, center, right or justify")
        span = _dominant_span([s for tl in el.lines for s in tl.spans])
        size = float(font_size or span.get("size") or 11)
        rgb = parse_color(color, _int_color_to_rgb(span.get("color", 0)))
        target = to_rect(rect) if rect is not None else fitz.Rect(el.rect)
        free = None
        if rect is None:
            # Text may grow into empty space to the right (single lines) and
            # below, stopping at neighbouring content or an enclosing box.
            free = _free_box(lay, el, self.doc[idx].rect)
            target.x1 += 2
            target.y1 = max(target.y1 + size * 0.25, free.y1)
        if line_height is None and len(el.lines) > 1:
            o0 = el.lines[0].spans[0]["origin"][1]
            o1 = el.lines[1].spans[0]["origin"][1]
            pitch = (o1 - o0) / size if size else 0
            line_height = pitch if 0.8 < pitch < 3 else None
        override = {}
        if font:
            override["family"] = font if font != "original" else "original"
        if bold is not None:
            override["bold"] = bold
        if italic is not None:
            override["italic"] = italic
        used = {}

        def run():
            pg = self.doc[idx]
            resolver = FontResolver(pg)  # before redaction drops unused fonts
            remove_text_lines(pg, el.lines)
            if font and font not in ("original", "sans", "serif", "mono"):
                fontname = base14_for(font, override.get("bold", False),
                                      override.get("italic", False))
                fnt = fitz.Font(fontname)
            else:
                fontname, fnt = resolver.resolve(span, new_text, override)
            if free is not None and new_text:
                need = max(fnt.text_length(t, fontsize=size)
                           for t in new_text.split("\n")) * 1.02 + 2
                single = len(el.lines) == 1 and "\n" not in new_text.strip()
                # widen single lines into free space; tolerate small metric
                # differences of substitute fonts for multi-line blocks
                limit = free.x1 if single else target.x1 + min(12, target.width * 0.05)
                if need > target.width:
                    grow = min(need, limit - target.x0) - target.width
                    if grow > 0:
                        if align == "right":
                            target.x0 -= grow
                        elif align == "center":
                            target.x0 -= grow / 2
                            target.x1 += grow / 2
                        else:
                            target.x1 += grow
            used["font"] = fontname
            used["size"] = _insert_fit(pg, target, new_text, size, rgb, fontname,
                                       _ALIGN[align], line_height)

        self.mutate(f"Edit text {id} on page {page}", run, [idx], focus=idx)
        res = {"page": page, "rect": rect_list(target), "font": used["font"],
               "font_size": round(used["size"], 2)}
        if rect is None and (target.x1 > el.rect.x1 + 4 or target.x0 < el.rect.x0 - 4):
            res["note"] = "The text box grew into free space to fit the new text."
        if used["size"] < size - 0.01:
            res["warning"] = (f"Text did not fit; font shrunk from {size:g} to "
                              f"{used['size']:.2f}pt. Pass a larger rect to keep the size.")
        return res

    def replace_text(self, find: str, replace: str, pages: Optional[Sequence[int]] = None,
                     match_case: bool = True) -> Dict:
        """Find & replace inside text lines, keeping each span's font/size/colour
        and re-flowing the rest of the line."""
        if not find:
            raise EngineError("find is empty")
        targets = list(pages or range(1, self.doc.page_count + 1))
        for p in targets:
            self.page_index(p)
        report = {"replacements": 0, "lines": [], "warnings": []}
        touched: List[int] = []

        def run():
            for p in targets:
                idx = p - 1
                pg = self.doc[idx]
                lay = build_layout(pg)
                jobs = []
                for el in lay.of_kind("line"):
                    tl = el.lines[0]
                    new_spans, count = _replace_in_spans(tl.spans, find, replace, match_case)
                    if count:
                        jobs.append((el, tl, new_spans, count))
                if not jobs:
                    continue
                touched.append(idx)
                for el, tl, _ns, _c in jobs:
                    if abs(tl.dir[0]) < 0.99:
                        report["warnings"].append(f"page {p} {el.id}: rotated text skipped")
                jobs = [j for j in jobs if abs(j[1].dir[0]) >= 0.99]
                fonts = FontResolver(pg)  # before redaction drops unused fonts
                remove_text_lines(pg, [j[1] for j in jobs])
                for el, tl, new_spans, count in jobs:
                    right = _reinsert_line(pg, fonts, tl, new_spans)
                    report["replacements"] += count
                    report["lines"].append({"page": p, "line_id": el.id,
                                            "text": "".join(t for _s, t in new_spans)})
                    over = right - tl.rect.x1
                    if over > 2 and right > pg.rect.width - 18:
                        report["warnings"].append(
                            f"page {p} {el.id}: line now ends at x={right:.0f}, near/over the "
                            "page edge; consider edit_text with a wider rect")
                    elif over > 2:
                        report["warnings"].append(
                            f"page {p} {el.id}: line grew {over:.0f}pt wider than before")

        if not any(self._line_has(p, find, match_case) for p in targets):
            return report
        self.mutate(f"Replace '{find}' with '{replace}'", run,
                    [p - 1 for p in targets], focus=targets[0] - 1)
        if not report["warnings"]:
            del report["warnings"]
        return report

    def _line_has(self, page: int, find: str, match_case: bool) -> bool:
        txt = self.doc[page - 1].get_text("text")
        return (find in txt) if match_case else (find.lower() in txt.lower())

    def add_text(self, page: int, rect, text: str, font_size: float = 11, color=None,
                 font: str = "sans", bold: bool = False, italic: bool = False,
                 align: str = "left", line_height: Optional[float] = None) -> Dict:
        idx = self.page_index(page)
        r = to_rect(rect)
        if align not in _ALIGN:
            raise EngineError("align must be left, center, right or justify")
        fontname = base14_for(font, bold, italic)
        rgb = parse_color(color)
        used = {}

        def run():
            used["size"] = _insert_fit(self.doc[idx], r, text, font_size, rgb, fontname,
                                       _ALIGN[align], line_height)

        self.mutate(f"Add text on page {page}", run, [idx], focus=idx)
        res = {"page": page, "rect": rect_list(r), "font_size": round(used["size"], 2)}
        if used["size"] < font_size - 0.01:
            res["warning"] = f"Text did not fit; font shrunk to {used['size']:.2f}pt"
        return res

    def add_html(self, page: int, rect, html: str, css: Optional[str] = None) -> Dict:
        idx = self.page_index(page)
        r = to_rect(rect)
        out = {}

        def run():
            spare, scale = self.doc[idx].insert_htmlbox(r, html, css=css)
            if spare < 0:
                raise EngineError("HTML could not be fitted into rect")
            out.update(spare_height=round(spare, 1), scale=round(scale, 3))

        self.mutate(f"Add HTML text on page {page}", run, [idx], focus=idx)
        res = {"page": page, "rect": rect_list(r), **out}
        if out["scale"] < 1:
            res["warning"] = f"Content was scaled down to {out['scale']:.0%} to fit"
        return res

    # ---- graphics & images --------------------------------------------
    def add_image(self, page: int, rect, image_path: str, keep_proportion: bool = True) -> Dict:
        idx = self.page_index(page)
        r = to_rect(rect)
        path = os.path.abspath(os.path.expanduser(image_path))
        if not os.path.isfile(path):
            raise EngineError(f"No such image file: {path}")
        self.mutate(f"Add image on page {page}",
                    lambda: self.doc[idx].insert_image(r, filename=path,
                                                       keep_proportion=keep_proportion),
                    [idx], focus=idx)
        return {"page": page, "rect": rect_list(r)}

    def add_shape(self, page: int, shape: str, rect, stroke_color="#000000",
                  fill_color=None, line_width: float = 1.0, opacity: float = 1.0) -> Dict:
        idx = self.page_index(page)
        r = to_rect(rect) if shape != "line" else None
        if shape not in ("rect", "ellipse", "line"):
            raise EngineError("shape must be rect, ellipse or line")
        stroke = parse_color(stroke_color, None)
        fill = parse_color(fill_color, None)

        def run():
            pg = self.doc[idx]
            kw = dict(color=stroke, fill=fill, width=line_width if stroke else 0,
                      fill_opacity=opacity, stroke_opacity=opacity)
            if shape == "line":
                x0, y0, x1, y1 = [float(v) for v in rect]
                pg.draw_line((x0, y0), (x1, y1), color=stroke or (0, 0, 0),
                             width=line_width, stroke_opacity=opacity)
            elif shape == "rect":
                pg.draw_rect(r, **kw)
            else:
                pg.draw_oval(r, **kw)

        self.mutate(f"Add {shape} on page {page}", run, [idx], focus=idx)
        return {"page": page, "shape": shape, "rect": [float(v) for v in rect]}

    def add_annotation(self, page: int, kind: str, rect, text: str = "", color=None) -> Dict:
        idx = self.page_index(page)
        r = to_rect(rect)

        def run():
            pg = self.doc[idx]
            if kind == "highlight":
                a = pg.add_highlight_annot(r)
            elif kind == "underline":
                a = pg.add_underline_annot(r)
            elif kind == "strikeout":
                a = pg.add_strikeout_annot(r)
            elif kind == "note":
                a = pg.add_text_annot(r.tl, text)
            elif kind == "freetext":
                a = pg.add_freetext_annot(r, text, fontsize=11,
                                          text_color=parse_color(color) or (0, 0, 0))
                return
            elif kind == "rect":
                a = pg.add_rect_annot(r)
            else:
                raise EngineError("kind must be highlight, underline, strikeout, note, "
                                  "freetext or rect")
            if color is not None and kind != "note":
                a.set_colors(stroke=parse_color(color))
            if text and kind != "note":
                a.set_info(content=text)
            a.update()

        self.mutate(f"Add {kind} annotation on page {page}", run, [idx], focus=idx)
        return {"page": page, "kind": kind, "rect": rect_list(r)}

    # ---- moving / re-layout -------------------------------------------
    def move_elements(self, page: int, ids: Sequence[str], dx: float = 0, dy: float = 0,
                      to_page: Optional[int] = None, scale: float = 1.0,
                      copy: bool = False) -> Dict:
        lay, els = self.resolve(page, ids)
        sel = _expand_selection(lay, els)
        box = _union([e.rect for e in sel])
        dest = fitz.Rect(box.x0 + dx, box.y0 + dy,
                         box.x0 + dx + box.width * scale, box.y0 + dy + box.height * scale)
        return self._relocate(page, lay, sel, box, dest, to_page, copy,
                              f"{'Copy' if copy else 'Move'} {', '.join(ids)} on page {page}")

    def move_region(self, page: int, rect, dx: float = 0, dy: float = 0,
                    to_page: Optional[int] = None, dest_rect=None, copy: bool = False) -> Dict:
        region = to_rect(rect)
        lay = self.layout(page)
        sel = _select_region(lay, region)
        if not sel:
            raise EngineError("Nothing found inside that region")
        if dest_rect is not None:
            dest = to_rect(dest_rect, "dest_rect")
        else:
            dest = region + (dx, dy, dx, dy)
        return self._relocate(page, lay, sel, region, dest, to_page, copy,
                              f"{'Copy' if copy else 'Move'} region on page {page}")

    def _relocate(self, page: int, lay: Layout, sel: List[Element], box: fitz.Rect,
                  dest: fitz.Rect, to_page: Optional[int], copy: bool, label: str) -> Dict:
        src_idx = self.page_index(page)
        dst_idx = self.page_index(to_page) if to_page else src_idx
        if box.width <= 0 or box.height <= 0:
            box = fitz.Rect(box.x0 - 0.5, box.y0 - 0.5, box.x1 + 0.5, box.y1 + 0.5)
        mat = fitz.Rect(box).torect(dest)
        content = [e for e in sel if e.kind in ("text", "line", "image", "drawing")]
        fields = [e for e in sel if e.kind == "field"]

        def run():
            notes = []
            if content:
                clip = _union([e.rect for e in content])
                src = _isolated_copy(self.doc, src_idx, lay, content, clip)
                if not copy:
                    pg = self.doc[src_idx]
                    remove_text_lines(pg, _unique_lines(
                        [tl for e in content if e.kind in ("text", "line") for tl in e.lines]))
                    for e in content:
                        if e.kind == "image":
                            remove_image(pg, e, lay)
                    drawings = [e for e in content if e.kind == "drawing"]
                    if drawings:
                        _apply(pg, [e.rect + (-1, -1, 1, 1) for e in drawings],
                               graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED)
                target = clip * mat
                _show_unrotated(self.doc[dst_idx], target, src, clip)
                src.close()
            for e in fields:
                notes.append(_move_widget(self.doc, src_idx, dst_idx, e, mat, copy))
            return [n for n in notes if n]

        touched = {src_idx, dst_idx}
        notes = self.mutate(label, run, touched, focus=dst_idx)
        res = {"moved" if not copy else "copied": [e.id for e in sel],
               "from": {"page": page, "rect": rect_list(box)},
               "to": {"page": dst_idx + 1, "rect": rect_list(box * mat)}}
        if notes:
            res["notes"] = notes
        return res

    # ---- forms ---------------------------------------------------------
    def list_fields(self, page: Optional[int] = None) -> List[Dict]:
        pages = [page] if page else range(1, self.doc.page_count + 1)
        out = []
        for p in pages:
            lay = self.layout(p)
            for e in lay.of_kind("field"):
                out.append({"page": p, "id": e.id, "bbox": rect_list(e.rect), **e.info})
        return out

    def _widgets_named(self, name: str) -> List[Tuple[int, fitz.Widget]]:
        # Keep each Page alive alongside its widgets: a Widget whose Page object
        # has been garbage-collected can no longer be updated.
        self._page_refs = [self.doc[i] for i in range(self.doc.page_count)]
        return [(p.number, w) for p in self._page_refs for w in p.widgets()
                if w.field_name == name]

    def fill_fields(self, values: Dict[str, object]) -> Dict:
        if not values:
            raise EngineError("values is empty")
        unknown = [n for n in values if not self._widgets_named(n)]
        if unknown:
            raise EngineError(f"No form field named: {', '.join(unknown)}. "
                              "Use list_form_fields to see names.")
        pages = set()

        def run():
            for name, value in values.items():
                for pno, w in self._widgets_named(name):
                    pages.add(pno)
                    _set_widget_value(w, value)

        self.mutate(f"Fill {len(values)} form field(s)", run, None)
        self._touch(pages)
        return {"filled": {n: self._value_of(n) for n in values}}

    def _value_of(self, name: str):
        vals = [w.field_value for _p, w in self._widgets_named(name)]
        return vals[0] if len(set(map(str, vals))) == 1 else vals

    def add_field(self, page: int, field_type: str, name: str, rect, value=None,
                  options: Optional[List[str]] = None, font_size: float = 0,
                  label: Optional[str] = None, required: bool = False,
                  read_only: bool = False, multiline: bool = False,
                  max_length: int = 0) -> Dict:
        idx = self.page_index(page)
        ftype = _FIELD_TYPES.get((field_type or "").lower())
        if ftype is None:
            raise EngineError(f"field_type must be one of {', '.join(_FIELD_TYPES)}")
        if not name:
            raise EngineError("name is required")
        if ftype in (fitz.PDF_WIDGET_TYPE_COMBOBOX, fitz.PDF_WIDGET_TYPE_LISTBOX) and not options:
            raise EngineError("combobox/listbox fields need options")
        r = to_rect(rect)

        def run():
            w = fitz.Widget()
            w.field_type = ftype
            w.field_name = name
            w.rect = r
            w.text_fontsize = font_size
            if options:
                w.choice_values = list(options)
            if label:
                w.field_label = label
            flags = 0
            if required:
                flags |= fitz.PDF_FIELD_IS_REQUIRED
            if read_only:
                flags |= fitz.PDF_FIELD_IS_READ_ONLY
            if multiline and ftype == fitz.PDF_WIDGET_TYPE_TEXT:
                flags |= fitz.PDF_TX_FIELD_IS_MULTILINE
            w.field_flags = flags
            if max_length and ftype == fitz.PDF_WIDGET_TYPE_TEXT:
                w.text_maxlen = max_length
            if value is not None:
                w.field_value = _coerce_value(ftype, value)
            elif ftype in (fitz.PDF_WIDGET_TYPE_CHECKBOX, fitz.PDF_WIDGET_TYPE_RADIOBUTTON):
                w.field_value = False
            self.doc[idx].add_widget(w)

        self.mutate(f"Add {field_type} field '{name}' on page {page}", run, [idx], focus=idx)
        return {"page": page, "name": name, "type": field_type, "rect": rect_list(r)}

    def _pick_widget(self, name: Optional[str], page: Optional[int], id: Optional[str]
                     ) -> Tuple[int, fitz.Widget]:
        if page and id:
            _lay, (el,) = self.resolve(page, [id])
            if el.kind != "field":
                raise EngineError(f"{id} is not a form field")
            pg = self.doc[page - 1]
            w = _find_widget(pg, el.xref)
            return page - 1, w
        if not name:
            raise EngineError("Give the field name, or page + id")
        found = self._widgets_named(name)
        if not found:
            raise EngineError(f"No form field named {name!r}")
        if len(found) > 1:
            raise EngineError(f"{len(found)} widgets are named {name!r} (e.g. a radio group); "
                              "identify one with page + id from list_form_fields")
        return found[0]

    def update_field(self, name: Optional[str] = None, page: Optional[int] = None,
                     id: Optional[str] = None, new_name: Optional[str] = None, rect=None,
                     value=None, options: Optional[List[str]] = None,
                     font_size: Optional[float] = None, label: Optional[str] = None,
                     required: Optional[bool] = None, read_only: Optional[bool] = None) -> Dict:
        pno, w = self._pick_widget(name, page, id)
        xref = w.xref
        r = to_rect(rect) if rect is not None else None

        def run():
            pg = self.doc[pno]
            wd = _find_widget(pg, xref)
            if new_name:
                wd.field_name = new_name
            if r is not None:
                wd.rect = r
            if options is not None:
                wd.choice_values = list(options)
            if font_size is not None:
                wd.text_fontsize = font_size
            if label is not None:
                wd.field_label = label
            flags = wd.field_flags
            if required is not None:
                flags = (flags | fitz.PDF_FIELD_IS_REQUIRED) if required else (
                    flags & ~fitz.PDF_FIELD_IS_REQUIRED)
            if read_only is not None:
                flags = (flags | fitz.PDF_FIELD_IS_READ_ONLY) if read_only else (
                    flags & ~fitz.PDF_FIELD_IS_READ_ONLY)
            wd.field_flags = flags
            if value is not None:
                _set_widget_value(wd, value, update=False)
            wd.update()
            return widget_info(wd), rect_list(wd.rect)

        info, box = self.mutate(f"Update field '{w.field_name}'", run, [pno], focus=pno)
        return {"page": pno + 1, "bbox": box, **info}

    def delete_field(self, name: Optional[str] = None, page: Optional[int] = None,
                     id: Optional[str] = None, all_named: bool = False) -> Dict:
        if all_named and name:
            targets = [(p, w.xref) for p, w in self._widgets_named(name)]
            if not targets:
                raise EngineError(f"No form field named {name!r}")
        else:
            pno, w = self._pick_widget(name, page, id)
            targets = [(pno, w.xref)]

        def run():
            for pno, xref in targets:
                pg = self.doc[pno]
                wd = _find_widget(pg, xref)
                if wd is not None:
                    pg.delete_widget(wd)

        self.mutate("Delete form field", run, {p for p, _ in targets}, focus=targets[0][0])
        return {"deleted_widgets": len(targets)}

    def flatten_fields(self) -> Dict:
        n = sum(1 for p in self.doc for _ in p.widgets())
        self.mutate("Flatten form fields", lambda: self.doc.bake(annots=False, widgets=True),
                    structural=True)
        return {"flattened_widgets": n}

    # ---- pages ---------------------------------------------------------
    def delete_pages(self, pages: Sequence[int]) -> Dict:
        idxs = sorted({self.page_index(p) for p in pages})
        if len(idxs) >= self.doc.page_count:
            raise EngineError("Cannot delete every page")
        self.mutate(f"Delete page(s) {_compact_pages([i + 1 for i in idxs])}",
                    lambda: self.doc.delete_pages(idxs), structural=True)
        return {"deleted": [i + 1 for i in idxs], "pages": self.doc.page_count}

    def reorder_pages(self, order: Sequence[int]) -> Dict:
        idxs = [self.page_index(p) for p in order]
        if not idxs:
            raise EngineError("order is empty")
        self.mutate("Reorder pages", lambda: self.doc.select(idxs), structural=True)
        return {"pages": self.doc.page_count, "order": list(order)}

    def move_page(self, page: int, to: int) -> Dict:
        n = self.doc.page_count
        src = self.page_index(page)
        self.page_index(to)
        order = [i for i in range(n) if i != src]
        order.insert(to - 1, src)
        self.mutate(f"Move page {page} to {to}", lambda: self.doc.select(order), structural=True)
        return {"moved": page, "now_at": to}

    def insert_blank_page(self, at: int, width: Optional[float] = None,
                          height: Optional[float] = None) -> Dict:
        n = self.doc.page_count
        if not 1 <= at <= n + 1:
            raise EngineError(f"at must be between 1 and {n + 1}")
        ref = self.doc[min(at - 1, n - 1)].rect if n else fitz.paper_rect("a4")
        w, h = width or ref.width, height or ref.height
        self.mutate(f"Insert blank page at {at}",
                    lambda: self.doc.new_page(pno=at - 1, width=w, height=h), structural=True)
        return {"inserted_at": at, "size": [w, h], "pages": self.doc.page_count}

    def duplicate_page(self, page: int, to: Optional[int] = None) -> Dict:
        idx = self.page_index(page)
        dest = (to - 1) if to else idx + 1
        self.mutate(f"Duplicate page {page}",
                    lambda: self.doc.fullcopy_page(idx, to=dest if dest < self.doc.page_count
                                                   else -1), structural=True)
        return {"copy_at": dest + 1, "pages": self.doc.page_count}

    def rotate_pages(self, pages: Sequence[int], angle: int) -> Dict:
        if angle % 90:
            raise EngineError("angle must be a multiple of 90")
        idxs = [self.page_index(p) for p in pages]

        def run():
            for i in idxs:
                self.doc[i].set_rotation((self.doc[i].rotation + angle) % 360)

        self.mutate(f"Rotate page(s) by {angle}", run, structural=True)
        return {"rotated": list(pages), "by": angle}

    def insert_pages_from(self, path: str, at: Optional[int] = None,
                          from_page: Optional[int] = None, to_page: Optional[int] = None,
                          password: Optional[str] = None) -> Dict:
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isfile(path):
            raise EngineError(f"No such file: {path}")
        src = fitz.open(path)
        if src.needs_pass and not (password and src.authenticate(password)):
            raise EngineError("Source document is password protected")
        n = self.doc.page_count
        at = at or n + 1
        if not 1 <= at <= n + 1:
            raise EngineError(f"at must be between 1 and {n + 1}")
        fp = (from_page or 1) - 1
        tp = (to_page or src.page_count) - 1
        try:
            self.mutate(f"Insert pages from {os.path.basename(path)}",
                        lambda: self.doc.insert_pdf(src, from_page=fp, to_page=tp,
                                                    start_at=at - 1), structural=True)
        finally:
            src.close()
        return {"inserted": tp - fp + 1, "at": at, "pages": self.doc.page_count}

    def extract_pages(self, pages: Sequence[int], output_path: str) -> Dict:
        idxs = [self.page_index(p) for p in pages]
        out_path = os.path.abspath(os.path.expanduser(output_path))
        out = fitz.open()
        try:
            for i in idxs:
                out.insert_pdf(self.doc, from_page=i, to_page=i)
            out.save(out_path, garbage=4, deflate=True)
        finally:
            out.close()
        return {"written": out_path, "pages": len(idxs)}

    def crop_page(self, page: int, rect) -> Dict:
        idx = self.page_index(page)
        r = to_rect(rect)
        self.mutate(f"Crop page {page}", lambda: self.doc[idx].set_cropbox(
            r & self.doc[idx].mediabox), structural=True)
        return {"page": page, "cropbox": rect_list(self.doc[idx].cropbox)}

    # ---- saving --------------------------------------------------------
    def save(self, output_path: Optional[str] = None, backup: bool = True) -> Dict:
        target = os.path.abspath(os.path.expanduser(output_path)) if output_path else self.path
        if not target.lower().endswith(".pdf"):
            raise EngineError("output_path must end in .pdf")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        backup_path = None
        if backup and target == self.path and os.path.exists(target):
            stem, ext = os.path.splitext(target)
            backup_path = f"{stem}.orig{ext}"
            if not os.path.exists(backup_path):
                shutil.copy2(target, backup_path)
            else:
                backup_path = None  # keep the first, truly-original backup
        fd, tmp = tempfile.mkstemp(suffix=".pdf", dir=os.path.dirname(target))
        os.close(fd)
        try:
            _save_compact(self.doc, tmp)
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        self.path = target
        self.dirty = False
        res = {"saved": target, "pages": self.doc.page_count,
               "bytes": os.path.getsize(target)}
        if backup_path:
            res["backup_of_original"] = backup_path
        return res

    def close(self) -> None:
        self.doc.close()


# ---- helpers used by the operations above --------------------------------
def _save_compact(doc: fitz.Document, path: str) -> None:
    """Save; if edits embedded whole font files (refnames QF*/QS*), subset the
    fonts in a copy first so the output stays small."""
    added = any(f[4].startswith(("QF", "QS")) for page in doc for f in page.get_fonts(full=True))
    if added:
        clone = fitz.open("pdf", doc.tobytes())
        try:
            clone.subset_fonts()
            clone.save(path, garbage=4, deflate=True)
            return
        except Exception:  # noqa: BLE001 - fall back to an unsubsetted save
            pass
        finally:
            clone.close()
    doc.save(path, garbage=4, deflate=True)


def _compact_pages(pages: Sequence[int]) -> str:
    out, start, prev = [], None, None
    for p in sorted(pages):
        if start is None:
            start = prev = p
        elif p == prev + 1:
            prev = p
        else:
            out.append(f"{start}-{prev}" if start != prev else f"{start}")
            start = prev = p
    if start is not None:
        out.append(f"{start}-{prev}" if start != prev else f"{start}")
    return ",".join(out)


def _union(rects: Iterable[fitz.Rect]) -> fitz.Rect:
    rects = [fitz.Rect(r) for r in rects]
    out = rects[0] if rects else fitz.Rect()
    for r in rects[1:]:
        out |= r
    return out


def _unique_lines(lines: Iterable[TextLine]) -> List[TextLine]:
    seen, out = set(), []
    for tl in lines:
        if tl.id not in seen:
            seen.add(tl.id)
            out.append(tl)
    return out


def _free_box(lay: Layout, el: Element, page_rect: fitz.Rect, margin: float = 24
              ) -> fitz.Rect:
    """How far ``el`` can grow right and down before touching other content,
    an enclosing box's edge, or the page margin."""
    r = el.rect
    own = {tl.id for tl in el.lines}
    others = [o for o in lay.elements.values()
              if o.kind not in ("text",) and o.id not in own and not o.info.get("background")]
    x1, y1 = page_rect.x1 - margin, page_rect.y1 - margin
    for o in others:
        if o.kind == "drawing" and o.rect.contains(r):
            x1, y1 = min(x1, o.rect.x1 - 2), min(y1, o.rect.y1 - 2)
        elif o.rect.y0 < r.y1 - 0.5 and o.rect.y1 > r.y0 + 0.5 and o.rect.x0 >= r.x1 - 1:
            x1 = min(x1, o.rect.x0 - 2)
    x1 = max(x1, r.x1)
    for o in others:
        if o.kind == "drawing" and o.rect.contains(r):
            continue
        if o.rect.x0 < x1 and o.rect.x1 > r.x0 and o.rect.y0 >= r.y1 - 1:
            y1 = min(y1, o.rect.y0 - 1)
    return fitz.Rect(r.x0, r.y0, x1, max(y1, r.y1))


def _expand_selection(lay: Layout, els: List[Element]) -> List[Element]:
    """Drop line ids already covered by a selected block, keep order."""
    blocks = {e.id for e in els if e.kind == "text"}
    return [e for e in els if not (e.kind == "line" and e.info.get("block") in blocks)]


def _select_region(lay: Layout, region: fitz.Rect, tol: float = 2.0) -> List[Element]:
    """Everything that belongs to a region: text lines whose centre is inside,
    images/drawings/fields lying (almost) entirely inside it."""
    loose = fitz.Rect(region.x0 - tol, region.y0 - tol, region.x1 + tol, region.y1 + tol)
    sel = []
    for e in lay.elements.values():
        centre = (e.rect.tl + e.rect.br) / 2
        if e.kind == "line" and region.contains(centre):
            sel.append(e)
        elif e.kind in ("image", "drawing") and loose.contains(e.rect) \
                and not e.info.get("background"):
            sel.append(e)
        elif e.kind == "field" and region.contains(centre):
            sel.append(e)
    return sel


def _isolated_copy(doc: fitz.Document, idx: int, lay: Layout, keep: List[Element],
                   clip: fitz.Rect) -> fitz.Document:
    """A one-page copy of page ``idx`` stripped down to the ``keep`` elements
    inside ``clip``, ready to be placed elsewhere with show_pdf_page."""
    src = fitz.open()
    src.insert_pdf(doc, from_page=idx, to_page=idx, annots=False, widgets=False)
    sp = src[0]
    sp.set_rotation(0)
    keep_ids = {e.id for e in keep}
    keep_lines = {tl.id for e in keep if e.kind in ("text", "line") for tl in e.lines}
    c = fitz.Rect(clip.x0 - 1, clip.y0 - 1, clip.x1 + 1, clip.y1 + 1)
    pr = sp.mediabox + (-10, -10, 10, 10)
    strips = [fitz.Rect(pr.x0, pr.y0, pr.x1, c.y0), fitz.Rect(pr.x0, c.y1, pr.x1, pr.y1),
              fitz.Rect(pr.x0, c.y0, c.x0, c.y1), fitz.Rect(c.x1, c.y0, pr.x1, c.y1)]
    strips = [s for s in strips if s.width > 0 and s.height > 0]
    # 1) everything that reaches outside the clip (backgrounds, neighbours)
    _apply(sp, strips, text=True, images=fitz.PDF_REDACT_IMAGE_REMOVE,
           graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED)
    # 2) other text lines inside the clip
    others = [e.lines[0] for e in lay.of_kind("line")
              if e.lines[0].id not in keep_lines and e.rect.intersects(c)]
    remove_text_lines(sp, others)
    kept_images = [e for e in keep if e.kind == "image"]
    other_images = [e for e in lay.of_kind("image") if e.id not in keep_ids
                    and c.contains(e.rect)
                    and not any(k.rect.intersects(e.rect) for k in kept_images)]
    _apply(sp, [e.rect for e in other_images], images=fitz.PDF_REDACT_IMAGE_REMOVE)
    # 3) other drawings fully inside the clip, unless they enclose kept content
    other_drawings = [e for e in lay.of_kind("drawing") if e.id not in keep_ids
                      and c.contains(e.rect)
                      and not any(e.rect.contains(k.rect) for k in keep)]
    _apply(sp, [e.rect + (-0.5, -0.5, 0.5, 0.5) for e in other_drawings],
           graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED)
    return src


def _show_unrotated(page: fitz.Page, target: fitz.Rect, src: fitz.Document,
                    clip: fitz.Rect) -> None:
    """show_pdf_page in unrotated page coordinates (matching our layouts)."""
    rot = page.rotation
    if rot:
        page.set_rotation(0)
    try:
        page.show_pdf_page(target, src, 0, clip=clip, keep_proportion=False)
    finally:
        if rot:
            page.set_rotation(rot)


def _move_widget(doc: fitz.Document, src_idx: int, dst_idx: int, el: Element,
                 mat: fitz.Matrix, copy: bool) -> Optional[str]:
    src_page, dst_page = doc[src_idx], doc[dst_idx]
    w = _find_widget(src_page, el.xref)
    if w is None:
        return None
    new_rect = fitz.Rect(w.rect) * mat
    if src_idx == dst_idx and not copy:
        w.rect = new_rect
        w.update()
        return None
    if w.field_type == fitz.PDF_WIDGET_TYPE_RADIOBUTTON:
        return f"{el.id}: radio buttons can't be moved/copied across pages; left in place"
    nw = fitz.Widget()
    for attr in ("field_type", "field_flags", "field_label", "text_fontsize", "text_font",
                 "text_color", "fill_color", "border_color", "border_width",
                 "choice_values", "text_maxlen", "button_caption"):
        try:
            setattr(nw, attr, getattr(w, attr))
        except Exception:  # noqa: BLE001
            pass
    nw.field_name = w.field_name if not copy else f"{w.field_name}_copy"
    nw.rect = new_rect
    nw.field_value = w.field_value
    if not copy:
        src_page.delete_widget(w)
    dst_page.add_widget(nw)
    return f"{el.id}: field '{nw.field_name}' placed on page {dst_idx + 1}"


def _coerce_value(ftype: int, value):
    if ftype == fitz.PDF_WIDGET_TYPE_CHECKBOX:
        if isinstance(value, str):
            return value.strip().lower() not in ("", "off", "false", "no", "0")
        return bool(value)
    if isinstance(value, bool):
        return "Yes" if value else "Off"
    return value if isinstance(value, list) else str(value)


def _set_widget_value(w: fitz.Widget, value, update: bool = True) -> None:
    if w.field_type == fitz.PDF_WIDGET_TYPE_RADIOBUTTON:
        # value selects the button whose on-state matches (or True/False)
        if isinstance(value, bool):
            w.field_value = value
        else:
            w.field_value = (str(value) == str(w.on_state()))
    elif w.field_type in (fitz.PDF_WIDGET_TYPE_COMBOBOX, fitz.PDF_WIDGET_TYPE_LISTBOX):
        opts = [o if isinstance(o, str) else o[0] for o in (w.choice_values or [])]
        if isinstance(value, str) and opts and value not in opts \
                and not (w.field_flags & fitz.PDF_CH_FIELD_IS_EDIT):
            raise EngineError(f"'{value}' is not an option of field '{w.field_name}': {opts}")
        w.field_value = value
    else:
        w.field_value = _coerce_value(w.field_type, value)
    if update:
        w.update()


def _insert_fit(page, rect, text, size, rgb, fontname, align, line_height) -> float:
    if line_height:
        # insert_textbox_fit doesn't take lineheight; do the same shrink loop here
        s = max(4.0, float(size))
        while True:
            rc = page.insert_textbox(rect, text, fontsize=s, fontname=fontname, color=rgb,
                                     align=align, lineheight=line_height)
            if rc >= 0 or s <= 4.0:
                break
            s = max(4.0, s * 0.92)
        if rc < 0:
            return insert_textbox_fit(page, rect, text, s, rgb, fontname, align)
        return s
    return insert_textbox_fit(page, rect, text, size, rgb, fontname=fontname, align=align)


def _replace_in_spans(spans: List[dict], find: str, repl: str, match_case: bool
                      ) -> Tuple[List[Tuple[dict, str]], int]:
    """Apply find/replace over a line's spans. Each replacement goes into the
    span where the match starts; matched chars are removed from their spans."""
    full = "".join(s["text"] for s in spans)
    hay, needle = (full, find) if match_case else (full.lower(), find.lower())
    owner = [i for i, s in enumerate(spans) for _ in s["text"]]
    out = [[] for _ in spans]
    pos, count = 0, 0
    while pos < len(full):
        j = hay.find(needle, pos)
        if j < 0:
            break
        for k in range(pos, j):
            out[owner[k]].append(full[k])
        out[owner[j]].append(repl)
        pos = j + len(find)
        count += 1
    for k in range(pos, len(full)):
        out[owner[k]].append(full[k])
    return [(s, "".join(parts)) for s, parts in zip(spans, out)], count


def _reinsert_line(page, fonts: FontResolver, tl: TextLine,
                   new_spans: List[Tuple[dict, str]]) -> float:
    """Write a line's spans back, each in its own style. Spans keep their
    original position until the first changed one; later spans flow on with
    the original gaps. Returns the new right edge."""
    x = None
    prev_end_orig = None
    changed = False
    for span, text in new_spans:
        ox, oy = span["origin"]
        if not changed and text != span["text"]:
            changed = True
        if x is None or not changed:
            x_here = ox
        else:
            gap = ox - prev_end_orig if prev_end_orig is not None else 0
            x_here = x + max(0.0, gap)
        prev_end_orig = span["bbox"][2]
        if not text:
            x = x_here
            continue
        fontname, font = fonts.resolve(span, text)
        page.insert_text((x_here, oy), text, fontname=fontname, fontsize=span["size"],
                         color=_int_color_to_rgb(span.get("color", 0)))
        x = x_here + font.text_length(text, fontsize=span["size"])
    return x or tl.rect.x1


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
