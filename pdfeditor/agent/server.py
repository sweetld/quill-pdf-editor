"""MCP server exposing Quill PDF Editor's editing engine to AI agents.

Run with ``python -m pdfeditor.agent`` (stdio transport). Every tool returns
compact JSON (render_page also returns a PNG image) and raises a tool error
with a plain-language message when something can't be done.
"""
from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import sys
import threading
from typing import Any, Dict, List, Literal, Optional

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .. import APP_NAME, __version__
from .engine import AgentDocument, EngineError, dumps, layout_to_dict, to_rect
from .render import render_page as _render_page

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIVE_DIR = os.path.join(os.path.expanduser("~/.cache/quill-pdf"), "live")

INSTRUCTIONS = """\
Quill PDF Editor tools let you view and edit existing PDF files.

Workflow:
1. open_pdf(path) -> page count, sizes, form fields.
2. Look before you edit: render_page(page) to see it, get_page_layout(page) for the
   structure. render_page(page, overlay=true) draws each element's id on the image;
   grid=true adds coordinate rulers for placing new content.
3. Edit with element ids from the latest get_page_layout of that page (ids like t3 =
   text block, t3.1 = one line of it, i0 image, g5 vector drawing, w2 form field,
   a1 annotation). Any edit to a page invalidates its ids; re-read the layout before
   the next id-based edit on that page (you'll get a clear error if you forget).
4. Verify with render_page after each meaningful change; undo() if it looks wrong.
5. save_pdf() overwrites the original (a one-time backup <name>.orig.pdf is made) or
   save_pdf(output_path=...) writes a new file. Nothing touches disk until you save.

Coordinates: PDF points (1/72 inch), origin top-left, y downward, rect = [x0,y0,x1,y1],
in the page's unrotated space (what layouts report). Pages are numbered from 1.

Choosing a tool:
- Remove content: delete_elements (precise) or delete_region (everything in a box).
  Deletion truly removes content; it never paints white boxes.
- Change wording: edit_text (rewrite one block/line, keeps font/size/colour; can reflow
  into a new rect) or replace_text (find & replace across pages, keeps per-span styling).
- Re-layout: move_elements / move_region move content as vector graphics with exact
  fonts (optionally to another page, scaled, or copied). Form fields inside move too.
- New content: add_text, add_html_text (rich text: bold/italic/lists/tables via HTML+CSS),
  add_image, add_shape, add_annotation.
- Forms: list_form_fields, fill_form, add_form_field, update_form_field (rename, move,
  resize, options), delete_form_field, flatten_form.
- Pages: delete/reorder/move/insert/duplicate/rotate/crop/extract.
- open_in_viewer shows the working copy in the Quill desktop app and follows your edits
  live, so the user can watch.
"""

mcp = MCPServer(name="quill-pdf", title=APP_NAME, version=__version__,
                instructions=INSTRUCTIONS)
_LOCK = threading.RLock()


# ---- open documents & the live viewer ------------------------------------
class Workspace:
    def __init__(self) -> None:
        self.docs: Dict[str, AgentDocument] = {}
        self.active: Optional[str] = None
        self.viewers: Dict[str, subprocess.Popen] = {}

    def get(self, key: Optional[str]) -> AgentDocument:
        if not self.docs:
            raise EngineError("No PDF is open. Call open_pdf(path) first.")
        key = key or self.active
        if key not in self.docs:
            raise EngineError(f"No open PDF called {key!r}; open: {', '.join(self.docs)}")
        return self.docs[key]

    def key_for(self, adoc: AgentDocument) -> str:
        return next(k for k, d in self.docs.items() if d is adoc)

    def open(self, path: str, password: Optional[str]) -> tuple:
        full = os.path.abspath(os.path.expanduser(path))
        for k, d in self.docs.items():
            if d.path == full:
                self.active = k
                return k, d, True
        adoc = AgentDocument(full, password)
        base = re.sub(r"[^\w.-]+", "_", os.path.splitext(os.path.basename(full))[0]) or "doc"
        key, n = base, 2
        while key in self.docs:
            key, n = f"{base}-{n}", n + 1
        adoc.on_change = self._on_change
        self.docs[key] = adoc
        self.active = key
        return key, adoc, False

    def close(self, key: str) -> None:
        adoc = self.docs.pop(key)
        adoc.close()
        proc = self.viewers.pop(key, None)
        if proc is not None and proc.poll() is None:
            proc.terminate()
        if self.active == key:
            self.active = next(iter(self.docs), None)

    # live preview: the viewer app watches this file and reloads on change
    def preview_path(self, key: str) -> str:
        return os.path.join(LIVE_DIR, f"{key}.live.pdf")

    def write_preview(self, key: str, adoc: AgentDocument, focus: Optional[int]) -> str:
        os.makedirs(LIVE_DIR, exist_ok=True)
        path = self.preview_path(key)
        tmp = path + ".tmp"
        adoc.doc.save(tmp, garbage=1)
        if focus is not None:
            with open(path + ".quill-focus", "w") as fh:
                json.dump({"page": focus + 1}, fh)
        os.replace(tmp, path)
        return path

    def _on_change(self, adoc: AgentDocument, focus: Optional[int]) -> None:
        key = self.key_for(adoc)
        proc = self.viewers.get(key)
        if proc is not None and proc.poll() is None:
            try:
                self.write_preview(key, adoc, focus)
            except Exception:  # noqa: BLE001 - preview is best-effort
                pass


WS = Workspace()


# ---- tool registration helper ---------------------------------------------
def tool(*, read_only: bool = False, destructive: bool = False, title: Optional[str] = None):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with _LOCK:
                try:
                    return fn(*args, **kwargs)
                except ToolError:
                    raise
                except EngineError as exc:
                    raise ToolError(str(exc)) from exc
                except Exception as exc:  # noqa: BLE001
                    raise ToolError(f"{type(exc).__name__}: {exc}") from exc

        mcp.add_tool(wrapper, title=title, structured_output=False,
                     annotations=ToolAnnotations(read_only_hint=read_only,
                                                 destructive_hint=destructive,
                                                 open_world_hint=False))
        return wrapper
    return deco


def _edit_result(adoc: AgentDocument, result: Dict, before: List[int]) -> str:
    after = adoc._page_rev
    if len(after) != len(before) or (after and min(after) > max(before, default=-1)):
        result["changed_pages"] = "all (re-read layouts before using ids)"
    else:
        changed = [i + 1 for i, (a, b) in enumerate(zip(after, before)) if a != b]
        if changed:
            result["changed_pages"] = changed
    result["can_undo"] = True
    return dumps(result)


def _edit(doc: Optional[str], op: str, *args, **kwargs) -> str:
    adoc = WS.get(doc)
    before = list(adoc._page_rev)
    result = getattr(adoc, op)(*args, **kwargs)
    return _edit_result(adoc, result, before)


DocArg = Optional[str]
Rect = List[float]


# ---- documents ------------------------------------------------------------
@tool(title="Open PDF")
def open_pdf(path: str, password: Optional[str] = None) -> str:
    """Open a PDF file for viewing/editing and make it the active document.

    Returns the document handle (`doc`, used to pick between several open PDFs),
    page count and sizes, form-field count and the outline. Opening a file that is
    already open just re-activates it (keeping unsaved edits)."""
    key, adoc, existed = WS.open(path, password)
    info = adoc.info()
    info["doc"] = key
    if existed:
        info["note"] = "Already open; re-activated (unsaved edits kept)"
    return dumps(info)


@tool(read_only=True, title="List open PDFs")
def list_open_pdfs() -> str:
    """List the PDFs currently open in this session and which one is active."""
    return dumps({"active": WS.active, "open": [
        {"doc": k, "path": d.path, "pages": d.doc.page_count, "unsaved_changes": d.dirty}
        for k, d in WS.docs.items()]})


@tool(read_only=True, title="Document info")
def get_document_info(doc: DocArg = None) -> str:
    """Page count, page sizes, rotation, metadata, outline, form-field count and the
    undo/redo history of an open PDF."""
    adoc = WS.get(doc)
    return dumps({"doc": WS.key_for(adoc), **adoc.info()})


@tool(title="Save PDF")
def save_pdf(output_path: Optional[str] = None, backup: bool = True, doc: DocArg = None) -> str:
    """Write the edited PDF to disk.

    Without output_path the original file is overwritten; with backup=true (default)
    the untouched original is first copied once to <name>.orig.pdf. With output_path
    the result goes to that new file, which becomes the document's path."""
    adoc = WS.get(doc)
    return dumps(adoc.save(output_path, backup))


@tool(title="Close PDF")
def close_pdf(discard_changes: bool = False, doc: DocArg = None) -> str:
    """Close an open PDF. Refuses if there are unsaved changes unless
    discard_changes=true."""
    adoc = WS.get(doc)
    key = WS.key_for(adoc)
    if adoc.dirty and not discard_changes:
        raise EngineError("There are unsaved changes; save_pdf first or pass "
                          "discard_changes=true")
    WS.close(key)
    return dumps({"closed": key, "active": WS.active})


@tool(title="Undo")
def undo(steps: int = 1, doc: DocArg = None) -> str:
    """Undo the last edit(s). All element ids become stale afterwards."""
    adoc = WS.get(doc)
    done = adoc.undo(steps)
    if not done:
        raise EngineError("Nothing to undo")
    return dumps({"undone": done, **adoc.history()})


@tool(title="Redo")
def redo(steps: int = 1, doc: DocArg = None) -> str:
    """Redo edit(s) previously undone."""
    adoc = WS.get(doc)
    done = adoc.redo(steps)
    if not done:
        raise EngineError("Nothing to redo")
    return dumps({"redone": done, **adoc.history()})


# ---- inspecting -------------------------------------------------------------
@tool(read_only=True, title="Render page")
def render_page(page: int, overlay: bool = False, grid: bool = False,
                region: Optional[Rect] = None, dpi: Optional[float] = None,
                doc: DocArg = None):
    """Render a page (with all current edits) as a PNG image so you can see it.

    overlay=true draws each element's box and id (blue=text, green=image,
    orange=drawing, purple=form field, red=annotation) and also makes those ids
    usable for editing. grid=true draws labelled coordinate lines (PDF points).
    region=[x0,y0,x1,y1] zooms into part of the page (good for checking detail).
    dpi overrides the automatic resolution (~1400px on the long side)."""
    adoc = WS.get(doc)
    idx = adoc.page_index(page)
    lay = adoc.layout(page) if overlay else None
    reg = to_rect(region, "region") if region is not None else None
    png, meta = _render_page(adoc.doc[idx], lay, reg, dpi, grid)
    meta["pages"] = adoc.doc.page_count
    return [dumps(meta), Image(data=png, format="png")]


@tool(read_only=True, title="Get page layout")
def get_page_layout(page: int, detail: Literal["blocks", "lines", "spans"] = "lines",
                    kinds: Optional[List[Literal["text", "images", "drawings", "fields",
                                                 "annotations"]]] = None,
                    region: Optional[Rect] = None, doc: DocArg = None) -> str:
    """Structured contents of a page with element ids, bounding boxes and styles.

    text: blocks (t0, t1...) with font, size, colour, bold/italic and their lines
    (t0.0, t0.1...); images (i0...) with pixel size; drawings (g0...) = vector paths
    with shape/fill/stroke (background=true for page-sized ones); fields (w0...) =
    form widgets with name/type/value; annotations (a0...).
    detail='spans' adds per-span styling for mixed-style lines; 'blocks' omits lines.
    kinds limits categories; region keeps only elements intersecting that rect."""
    adoc = WS.get(doc)
    lay = adoc.layout(page)
    reg = to_rect(region, "region") if region is not None else None
    return dumps(layout_to_dict(lay, detail, kinds, reg))


@tool(read_only=True, title="Get text")
def get_text(pages: Optional[List[int]] = None, doc: DocArg = None) -> str:
    """Plain text of the given pages (default: all), in reading order."""
    adoc = WS.get(doc)
    return dumps({str(k): v for k, v in adoc.page_text(pages).items()})


@tool(read_only=True, title="Find text")
def find_text(query: str, page: Optional[int] = None, match_case: bool = False,
              doc: DocArg = None) -> str:
    """Search for text. Each hit gives page, bbox, and the line_id/block_id containing
    it (usable directly with edit_text, delete_elements, move_elements)."""
    adoc = WS.get(doc)
    hits = adoc.find_text(query, page, match_case)
    return dumps({"count": len(hits), "hits": hits[:200]})


# ---- deleting -------------------------------------------------------------
@tool(destructive=True, title="Delete elements")
def delete_elements(page: int, ids: List[str], doc: DocArg = None) -> str:
    """Remove specific elements (ids from get_page_layout): text blocks/lines, images,
    vector drawings, form fields or annotations. Content underneath stays intact
    (no white box). Deleting a drawing also removes smaller drawings fully inside it."""
    return _edit(doc, "delete_elements", page, ids)


@tool(destructive=True, title="Delete region")
def delete_region(page: int, rect: Rect, text: bool = True, images: bool = True,
                  graphics: bool = True, fields: bool = False, annotations: bool = False,
                  doc: DocArg = None) -> str:
    """Remove everything inside a rectangle: text touching it, image pixels inside
    it, vector paths fully inside it, and optionally form fields / annotations whose
    centre is inside. Use the flags to keep some kinds (e.g. graphics=false to keep
    lines and boxes while removing text)."""
    return _edit(doc, "delete_region", page, rect, text, images, graphics, fields, annotations)


# ---- text -----------------------------------------------------------------
@tool(title="Edit text")
def edit_text(page: int, id: str, new_text: str, rect: Optional[Rect] = None,
              font_size: Optional[float] = None, color: Optional[str] = None,
              font: Optional[Literal["original", "sans", "serif", "mono"]] = None,
              bold: Optional[bool] = None, italic: Optional[bool] = None,
              align: Literal["left", "center", "right", "justify"] = "left",
              line_height: Optional[float] = None, doc: DocArg = None) -> str:
    """Replace the text of a block (t3) or line (t3.1) with new_text, keeping its
    font, size and colour unless overridden. Use '\\n' for line breaks; text wraps
    within the rect. rect (default: the element's own box) lets you reflow the text
    into a different area. If it doesn't fit, the font is shrunk and a warning says
    so — give a bigger rect if that's not wanted. The original embedded font is
    reused when it has all needed glyphs, otherwise the closest standard font
    (sans/serif/mono, bold/italic) is used. Empty new_text just deletes."""
    return _edit(doc, "edit_text", page, id, new_text, rect, font_size, color, font, bold,
                 italic, align, line_height)


@tool(title="Find and replace text")
def replace_text(find: str, replace: str, pages: Optional[List[int]] = None,
                 match_case: bool = True, doc: DocArg = None) -> str:
    """Find & replace across pages (default: all), within single lines. Each span
    keeps its own font/size/colour; text after the change re-flows along the line.
    Reports each changed line and warns when a line grew wider (check with
    render_page, and use edit_text with a wider rect if it now overlaps)."""
    return _edit(doc, "replace_text", find, replace, pages, match_case)


@tool(title="Add text")
def add_text(page: int, rect: Rect, text: str, font_size: float = 11,
             color: str = "#000000", font: Literal["sans", "serif", "mono"] = "sans",
             bold: bool = False, italic: bool = False,
             align: Literal["left", "center", "right", "justify"] = "left",
             line_height: Optional[float] = None, doc: DocArg = None) -> str:
    """Add new plain text wrapped inside rect (top-aligned). Shrinks the font with a
    warning if it doesn't fit. line_height is a multiple of font size (e.g. 1.4)."""
    return _edit(doc, "add_text", page, rect, text, font_size, color, font, bold, italic,
                 align, line_height)


@tool(title="Add rich (HTML) text")
def add_html_text(page: int, rect: Rect, html: str, css: Optional[str] = None,
                  doc: DocArg = None) -> str:
    """Add rich text laid out from HTML+CSS inside rect: headings, bold/italic, lists,
    simple tables, colours, alignment. Content is scaled down to fit if needed
    (reported as scale). Example css: '* {font-family: sans-serif; font-size: 10pt}'."""
    return _edit(doc, "add_html", page, rect, html, css)


# ---- graphics -------------------------------------------------------------
@tool(title="Add image")
def add_image(page: int, rect: Rect, image_path: str, keep_proportion: bool = True,
              doc: DocArg = None) -> str:
    """Place an image file (PNG/JPEG/...) into rect on a page."""
    return _edit(doc, "add_image", page, rect, image_path, keep_proportion)


@tool(title="Add shape")
def add_shape(page: int, shape: Literal["rect", "ellipse", "line"], rect: Rect,
              stroke_color: Optional[str] = "#000000", fill_color: Optional[str] = None,
              line_width: float = 1.0, opacity: float = 1.0, doc: DocArg = None) -> str:
    """Draw a rectangle, ellipse or line (for a line, rect = [x_start, y_start, x_end,
    y_end]). Colours are '#rrggbb' or 'none'. A filled rect with stroke 'none' makes a
    box/background or a cover-up panel."""
    return _edit(doc, "add_shape", page, shape, rect, stroke_color, fill_color, line_width,
                 opacity)


@tool(title="Add annotation")
def add_annotation(page: int, kind: Literal["highlight", "underline", "strikeout", "note",
                                            "freetext", "rect"],
                   rect: Rect, text: str = "", color: Optional[str] = None,
                   doc: DocArg = None) -> str:
    """Add a review annotation: highlight/underline/strikeout over rect, a sticky
    note at rect's top-left with text, a freetext comment box, or a rect outline."""
    return _edit(doc, "add_annotation", page, kind, rect, text, color)


# ---- re-layout ------------------------------------------------------------
@tool(title="Move elements")
def move_elements(page: int, ids: List[str], dx: float = 0, dy: float = 0,
                  to_page: Optional[int] = None, scale: float = 1.0, copy: bool = False,
                  doc: DocArg = None) -> str:
    """Move (or copy) elements by (dx, dy) points, optionally onto another page and/or
    scaled (scale is applied around the group's top-left). Text, images and drawings
    move as exact vector content (same fonts); form fields are re-positioned. Elements
    keep their positions relative to each other."""
    return _edit(doc, "move_elements", page, ids, dx, dy, to_page, scale, copy)


@tool(title="Move region")
def move_region(page: int, rect: Rect, dx: float = 0, dy: float = 0,
                to_page: Optional[int] = None, dest_rect: Optional[Rect] = None,
                copy: bool = False, doc: DocArg = None) -> str:
    """Move (or copy) everything in a region: text lines whose centre is inside, and
    images/drawings/form fields inside it. Either shift by (dx, dy) or fit the region
    into dest_rect (which scales it), optionally on to_page. Page-sized backgrounds
    are left alone. Ideal for re-laying out sections, columns or form blocks."""
    return _edit(doc, "move_region", page, rect, dx, dy, to_page, dest_rect, copy)


# ---- forms ----------------------------------------------------------------
@tool(read_only=True, title="List form fields")
def list_form_fields(page: Optional[int] = None, doc: DocArg = None) -> str:
    """All form fields (or those on one page): name, type, current value, options,
    on_state for checkboxes/radios, flags and position (with the page-local id)."""
    adoc = WS.get(doc)
    fields = adoc.list_fields(page)
    return dumps({"count": len(fields), "fields": fields})


@tool(title="Fill form")
def fill_form(values: Dict[str, Any], doc: DocArg = None) -> str:
    """Set form field values by field name: text -> string, checkbox -> true/false,
    radio group -> the on_state of the button to select, combobox/listbox -> one of
    its options."""
    return _edit(doc, "fill_fields", values)


@tool(title="Add form field")
def add_form_field(page: int, field_type: Literal["text", "checkbox", "radio", "combobox",
                                                  "listbox", "button", "signature"],
                   name: str, rect: Rect, value: Optional[Any] = None,
                   options: Optional[List[str]] = None, font_size: float = 0,
                   label: Optional[str] = None, required: bool = False,
                   read_only: bool = False, multiline: bool = False, max_length: int = 0,
                   doc: DocArg = None) -> str:
    """Create a new interactive form field. font_size 0 = auto. label is the tooltip.
    Radio buttons sharing a name form a group."""
    return _edit(doc, "add_field", page, field_type, name, rect, value, options, font_size,
                 label, required, read_only, multiline, max_length)


@tool(title="Update form field")
def update_form_field(name: Optional[str] = None, page: Optional[int] = None,
                      id: Optional[str] = None, new_name: Optional[str] = None,
                      rect: Optional[Rect] = None, value: Optional[Any] = None,
                      options: Optional[List[str]] = None, font_size: Optional[float] = None,
                      label: Optional[str] = None, required: Optional[bool] = None,
                      read_only: Optional[bool] = None, doc: DocArg = None) -> str:
    """Change a form field: rename, move/resize (rect), value, choice options, font
    size, tooltip label, required/read-only flags. Identify it by name, or by page +
    id (needed when several widgets share a name, e.g. radio groups)."""
    return _edit(doc, "update_field", name, page, id, new_name, rect, value, options,
                 font_size, label, required, read_only)


@tool(destructive=True, title="Delete form field")
def delete_form_field(name: Optional[str] = None, page: Optional[int] = None,
                      id: Optional[str] = None, all_named: bool = False,
                      doc: DocArg = None) -> str:
    """Delete a form field by name (or page + id). all_named=true deletes every widget
    with that name (e.g. a whole radio group)."""
    return _edit(doc, "delete_field", name, page, id, all_named)


@tool(destructive=True, title="Flatten form")
def flatten_form(doc: DocArg = None) -> str:
    """Burn all form fields (with their current values) into the page content so they
    are no longer editable."""
    return _edit(doc, "flatten_fields")


# ---- pages ----------------------------------------------------------------
@tool(destructive=True, title="Delete pages")
def delete_pages(pages: List[int], doc: DocArg = None) -> str:
    """Delete pages (1-based numbers)."""
    return _edit(doc, "delete_pages", pages)


@tool(title="Reorder pages")
def reorder_pages(order: List[int], doc: DocArg = None) -> str:
    """Rearrange pages: order lists the current page numbers in their new order.
    Pages left out are dropped; repeated numbers duplicate pages."""
    return _edit(doc, "reorder_pages", order)


@tool(title="Move page")
def move_page(page: int, to: int, doc: DocArg = None) -> str:
    """Move one page so it becomes page number `to`."""
    return _edit(doc, "move_page", page, to)


@tool(title="Insert blank page")
def insert_blank_page(at: int, width: Optional[float] = None, height: Optional[float] = None,
                      doc: DocArg = None) -> str:
    """Insert a blank page so it becomes page number `at` (page_count+1 appends).
    Size defaults to the neighbouring page's size."""
    return _edit(doc, "insert_blank_page", at, width, height)


@tool(title="Duplicate page")
def duplicate_page(page: int, to: Optional[int] = None, doc: DocArg = None) -> str:
    """Copy a page (default: right after itself, or at position `to`)."""
    return _edit(doc, "duplicate_page", page, to)


@tool(title="Rotate pages")
def rotate_pages(pages: List[int], angle: Literal[90, 180, 270, -90] = 90,
                 doc: DocArg = None) -> str:
    """Rotate pages clockwise by angle (relative to their current rotation)."""
    return _edit(doc, "rotate_pages", pages, angle)


@tool(title="Insert pages from PDF")
def insert_pages_from_pdf(path: str, at: Optional[int] = None,
                          from_page: Optional[int] = None, to_page: Optional[int] = None,
                          doc: DocArg = None) -> str:
    """Insert pages from another PDF file (default: all of it, appended at the end)."""
    return _edit(doc, "insert_pages_from", path, at, from_page, to_page)


@tool(title="Crop page")
def crop_page(page: int, rect: Rect, doc: DocArg = None) -> str:
    """Set the visible area (crop box) of a page to rect."""
    return _edit(doc, "crop_page", page, rect)


@tool(title="Extract pages to file")
def extract_pages(pages: List[int], output_path: str, doc: DocArg = None) -> str:
    """Write a copy of the given pages (current edited state) to a new PDF file.
    Doesn't change the open document."""
    adoc = WS.get(doc)
    return dumps(adoc.extract_pages(pages, output_path))


# ---- live viewer ----------------------------------------------------------
@tool(read_only=True, title="Open in Quill viewer")
def open_in_viewer(page: Optional[int] = None, doc: DocArg = None) -> str:
    """Show the current working copy in the Quill PDF Editor desktop window so the
    user can watch. The window follows every later edit live (jumping to the edited
    page). It shows a preview copy: the original file is only written by save_pdf."""
    adoc = WS.get(doc)
    key = WS.key_for(adoc)
    focus = adoc.page_index(page) if page else 0
    path = WS.write_preview(key, adoc, focus)
    proc = WS.viewers.get(key)
    if proc is not None and proc.poll() is None:
        return dumps({"viewer": "already open; showing latest", "preview_file": path})
    env = _gui_env()
    if env is None:
        raise EngineError("No graphical display available to this server (DISPLAY / "
                          "WAYLAND_DISPLAY unset and no local display found)")
    cmd = [sys.executable, "-m", "pdfeditor", path, "--page", str(focus + 1)]
    WS.viewers[key] = subprocess.Popen(cmd, cwd=PROJECT_ROOT, env=env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       start_new_session=True)
    return dumps({"viewer": "launched", "preview_file": path})


def _gui_env() -> Optional[Dict[str, str]]:
    """Environment for launching the desktop viewer. MCP clients often start
    servers with a trimmed environment, so look for the local display if the
    usual variables are missing."""
    env = dict(os.environ)
    if env.get("DISPLAY") or env.get("WAYLAND_DISPLAY") or \
            env.get("QT_QPA_PLATFORM") == "offscreen":
        return env
    runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    found = False
    if os.path.exists(os.path.join(runtime, "wayland-0")):
        env.update(WAYLAND_DISPLAY="wayland-0", XDG_RUNTIME_DIR=runtime)
        found = True
    x11 = sorted(n for n in os.listdir("/tmp/.X11-unix")) if os.path.isdir("/tmp/.X11-unix") else []
    if x11:
        env["DISPLAY"] = ":" + x11[0].lstrip("X")
        found = True
    return env if found else None


def main() -> None:
    mcp.run()
