"""PdfDocument — owns the single fitz.Document and exposes high-level operations.

All PyMuPDF access is serialized through ``lock`` (a QMutex) because PyMuPDF is
not thread-safe: the background page renderer and any edit operations must not
touch the document concurrently.
"""
from __future__ import annotations

import os
from typing import List, Optional, Tuple

import fitz
from PySide6.QtCore import QMutexLocker, QObject, QRecursiveMutex, Signal


class PdfDocument(QObject):
    """A thin, signal-emitting wrapper around ``fitz.Document``."""

    documentChanged = Signal()          # a new document was opened/closed
    dirtyChanged = Signal(bool)         # unsaved-changes flag flipped
    structureChanged = Signal()         # pages added/removed/reordered/rotated
    pageContentChanged = Signal(int)    # a single page's content was edited

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._doc: Optional[fitz.Document] = None
        self._path: Optional[str] = None
        self._dirty = False
        # Shared with PageRenderer so rendering and editing never overlap.
        self.lock = QRecursiveMutex()

    # ---- state -----------------------------------------------------------
    @property
    def doc(self) -> Optional[fitz.Document]:
        return self._doc

    @property
    def path(self) -> Optional[str]:
        return self._path

    @property
    def is_open(self) -> bool:
        return self._doc is not None

    @property
    def page_count(self) -> int:
        with QMutexLocker(self.lock):
            return self._doc.page_count if self._doc else 0

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    def display_name(self) -> str:
        if self._path:
            return os.path.basename(self._path)
        return "Untitled" if self._doc else ""

    def set_dirty(self, value: bool = True) -> None:
        if value != self._dirty:
            self._dirty = value
            self.dirtyChanged.emit(value)

    # ---- open / close ----------------------------------------------------
    def open(self, path: str, password: Optional[str] = None) -> None:
        doc = fitz.open(path)
        if doc.needs_pass:
            if password is None or not doc.authenticate(password):
                doc.close()
                raise PermissionError("Document is password protected")
        self._close_locked()
        self._doc = doc
        self._path = path
        self._dirty = False
        self.dirtyChanged.emit(False)
        self.documentChanged.emit()

    def close(self) -> None:
        with QMutexLocker(self.lock):
            self._close_locked()
        self.documentChanged.emit()

    def _close_locked(self) -> None:
        if self._doc is not None:
            self._doc.close()
        self._doc = None
        self._path = None
        self._dirty = False

    # ---- page geometry / metadata ---------------------------------------
    def page_size_pt(self, index: int) -> Tuple[float, float]:
        """(width, height) of a page in PDF points, accounting for rotation."""
        with QMutexLocker(self.lock):
            rect = self._doc[index].rect
            return (rect.width, rect.height)

    def page_sizes_pt(self) -> List[Tuple[float, float]]:
        with QMutexLocker(self.lock):
            return [(p.rect.width, p.rect.height) for p in self._doc]

    def get_toc(self) -> list:
        """Table of contents: list of [level, title, page_number(1-based), ...]."""
        with QMutexLocker(self.lock):
            return self._doc.get_toc(simple=True) if self._doc else []

    def metadata(self) -> dict:
        with QMutexLocker(self.lock):
            return dict(self._doc.metadata) if self._doc else {}

    # ---- text extraction / search ---------------------------------------
    def page_words(self, index: int) -> list:
        """List of (x0, y0, x1, y1, word, block_no, line_no, word_no)."""
        with QMutexLocker(self.lock):
            return self._doc[index].get_text("words")

    def textbox(self, index: int, rect: fitz.Rect) -> str:
        with QMutexLocker(self.lock):
            return self._doc[index].get_textbox(rect)

    def page_text(self, index: int) -> str:
        with QMutexLocker(self.lock):
            return self._doc[index].get_text("text")

    def search_page(self, index: int, needle: str) -> List[fitz.Rect]:
        """Return match rectangles for ``needle`` on a page (case-insensitive)."""
        if not needle:
            return []
        with QMutexLocker(self.lock):
            return list(self._doc[index].search_for(needle))

    # ---- page management primitives -------------------------------------
    # These are the low-level operations that the QUndoCommands in
    # document/undo.py compose into undoable actions. Each mutates the
    # document, flags it dirty, and emits structureChanged so the UI reloads.
    def _after_structure_change(self) -> None:
        self.set_dirty(True)
        self.structureChanged.emit()

    def rotation(self, index: int) -> int:
        with QMutexLocker(self.lock):
            return self._doc[index].rotation

    def set_rotation(self, index: int, angle: int) -> None:
        with QMutexLocker(self.lock):
            self._doc[index].set_rotation(angle % 360)
        self._after_structure_change()

    def reorder(self, order: List[int]) -> None:
        """Reorder/duplicate/subset pages to ``order`` (list of page indices)."""
        with QMutexLocker(self.lock):
            self._doc.select(order)
        self._after_structure_change()

    def delete_pages(self, indices: List[int]) -> None:
        with QMutexLocker(self.lock):
            for idx in sorted(indices, reverse=True):
                self._doc.delete_page(idx)
        self._after_structure_change()

    def insert_blank(self, at: int, width: float, height: float) -> None:
        with QMutexLocker(self.lock):
            self._doc.new_page(pno=at, width=width, height=height)
        self._after_structure_change()

    def duplicate_page(self, index: int) -> None:
        with QMutexLocker(self.lock):
            self._doc.fullcopy_page(index, to=index + 1)
        self._after_structure_change()

    def extract_page_doc(self, index: int) -> "fitz.Document":
        """A new in-memory single-page document (used to make delete undoable)."""
        with QMutexLocker(self.lock):
            tmp = fitz.open()
            tmp.insert_pdf(self._doc, from_page=index, to_page=index)
        return tmp

    def insert_doc(self, src: "fitz.Document", at: int,
                   from_page: int = 0, to_page: Optional[int] = None) -> None:
        with QMutexLocker(self.lock):
            last = src.page_count - 1 if to_page is None else to_page
            self._doc.insert_pdf(src, from_page=from_page, to_page=last, start_at=at)
        self._after_structure_change()

    # ---- text-content editing (redact + reinsert) -----------------------
    def text_blocks(self, index: int) -> list:
        """List of (x0, y0, x1, y1, text, block_no, block_type)."""
        with QMutexLocker(self.lock):
            return self._doc[index].get_text("blocks")

    def estimate_fontsize(self, index: int, rect: "fitz.Rect") -> float:
        """Median span size found inside ``rect`` (fallback 11pt)."""
        with QMutexLocker(self.lock):
            data = self._doc[index].get_text("dict", clip=rect)
        sizes = [span["size"] for block in data.get("blocks", [])
                 for line in block.get("lines", []) for span in line.get("spans", [])]
        return sorted(sizes)[len(sizes) // 2] if sizes else 11.0

    def replace_text_block(self, index: int, rect: "fitz.Rect", new_text: str,
                           fontsize: float, color, fontname: str = "helv") -> None:
        from ..textfit import insert_textbox_fit
        with QMutexLocker(self.lock):
            page = self._doc[index]
            page.add_redact_annot(rect)
            page.apply_redactions()
            insert_textbox_fit(page, rect, new_text, fontsize, color, fontname=fontname)
        self.set_dirty(True)
        self.pageContentChanged.emit(index)

    def text_line_at(self, index: int, pt: "fitz.Point"):
        """The text line under a point, or None.

        Returns (rect, text, fontsize, color_int, flags, fontname) from the
        line's first span so the caller can preserve size/colour/bold/italic.
        """
        with QMutexLocker(self.lock):
            data = self._doc[index].get_text("dict")
        for block in data.get("blocks", []):
            if block.get("type", 0) != 0:  # 0 == text block
                continue
            for line in block.get("lines", []):
                rect = fitz.Rect(line["bbox"])
                if not rect.contains(pt):
                    continue
                spans = line.get("spans", [])
                text = "".join(s.get("text", "") for s in spans)
                if not text.strip() or not spans:
                    continue
                first = spans[0]
                return (rect, text, first.get("size", 11.0), first.get("color", 0),
                        first.get("flags", 0), first.get("font", ""))
        return None

    def image_at(self, index: int, pt: "fitz.Point"):
        """The smallest image under a point: (rect, image_bytes) or None."""
        with QMutexLocker(self.lock):
            infos = self._doc[index].get_image_info(xrefs=True)
        best = None
        for info in infos:
            r = fitz.Rect(info["bbox"])
            if r.contains(pt) and r.width > 0 and r.height > 0:
                if best is None or r.get_area() < fitz.Rect(best["bbox"]).get_area():
                    best = info
        if best is None:
            return None
        xref = best.get("xref", 0)
        if not xref:
            return None
        with QMutexLocker(self.lock):
            data = self._doc.extract_image(xref)
        if not data or "image" not in data:
            return None
        return fitz.Rect(best["bbox"]), data["image"]

    def graphics_at(self, index: int, pt: "fitz.Point"):
        """The smallest vector drawing under a point: (rect, [path_dict]) or None."""
        with QMutexLocker(self.lock):
            drawings = self._doc[index].get_drawings()
        best = None
        best_area = None
        for d in drawings:
            r = fitz.Rect(d["rect"])
            if r.contains(pt) and r.width > 1 and r.height > 1:
                area = r.get_area()
                if best_area is None or area < best_area:
                    best, best_area = d, area
        if best is None:
            return None
        return fitz.Rect(best["rect"]), [best]

    def text_block_at(self, index: int, pt: "fitz.Point"):
        """The text block under a point, or None.

        Returns (rect, text, fontsize, color_int, flags, fontname) using the
        block's first span for style, so an edit can preserve it.
        """
        with QMutexLocker(self.lock):
            data = self._doc[index].get_text("dict")
        for block in data.get("blocks", []):
            if block.get("type", 0) != 0:
                continue
            rect = fitz.Rect(block["bbox"])
            if not rect.contains(pt):
                continue
            lines = block.get("lines", [])
            spans = [s for line in lines for s in line.get("spans", [])]
            text = "\n".join("".join(s.get("text", "") for s in line.get("spans", []))
                             for line in lines)
            if not text.strip() or not spans:
                continue
            first = spans[0]
            return (rect, text, first.get("size", 11.0), first.get("color", 0),
                    first.get("flags", 0), first.get("font", ""))
        return None

    def redact_region(self, index: int, rect: "fitz.Rect",
                      images: bool = False, graphics: bool = False) -> None:
        """Remove content inside ``rect``. By default only text/graphics that
        the redaction covers; set flags to also drop images or covered line-art."""
        with QMutexLocker(self.lock):
            page = self._doc[index]
            page.add_redact_annot(rect)
            kwargs = {}
            if images:
                kwargs["images"] = fitz.PDF_REDACT_IMAGE_REMOVE
            if graphics:
                kwargs["graphics"] = fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED
            page.apply_redactions(**kwargs)
        self.set_dirty(True)
        self.pageContentChanged.emit(index)

    def page_snapshot(self, index: int) -> "fitz.Document":
        """A one-page copy of a page, used to make content edits undoable."""
        with QMutexLocker(self.lock):
            tmp = fitz.open()
            tmp.insert_pdf(self._doc, from_page=index, to_page=index)
        return tmp

    def replace_page(self, index: int, src: "fitz.Document") -> None:
        with QMutexLocker(self.lock):
            self._doc.delete_page(index)
            self._doc.insert_pdf(src, from_page=0, to_page=0, start_at=index)
        self.set_dirty(True)
        self.pageContentChanged.emit(index)

    # ---- export (not undoable — produces new files) ---------------------
    def export_pages(self, indices: List[int], path: str) -> None:
        out = fitz.open()
        try:
            with QMutexLocker(self.lock):
                for idx in indices:
                    out.insert_pdf(self._doc, from_page=idx, to_page=idx)
            out.save(path, garbage=4, deflate=True)
        finally:
            out.close()

    def split_to_folder(self, folder: str, stem: str) -> List[str]:
        """Write one single-page PDF per page into ``folder``; return the paths."""
        written: List[str] = []
        n = self.page_count
        width = len(str(n))
        for idx in range(n):
            out = fitz.open()
            try:
                with QMutexLocker(self.lock):
                    out.insert_pdf(self._doc, from_page=idx, to_page=idx)
                dest = os.path.join(folder, f"{stem}_{idx + 1:0{width}d}.pdf")
                out.save(dest, garbage=4, deflate=True)
                written.append(dest)
            finally:
                out.close()
        return written

    # ---- save ------------------------------------------------------------
    def can_save_incremental(self) -> bool:
        return bool(self._path) and self._path.lower().endswith(".pdf")

    def _save_clone_with(self, path: str, bake_fn) -> None:
        """Save a full clone of the document with ``bake_fn`` applied to it.

        Used when there is an editable-object layer to flatten into the output:
        the in-memory document (and its live objects) is left untouched.
        """
        data = self._doc.tobytes()
        clone = fitz.open("pdf", data)
        try:
            bake_fn(clone)
            clone.save(path, garbage=4, deflate=True)
        finally:
            clone.close()

    def save(self, bake_fn=None) -> None:
        if not self._path:
            raise ValueError("No path set; use save_as()")
        with QMutexLocker(self.lock):
            if bake_fn is not None:
                self._save_clone_with(self._path, bake_fn)
            else:
                self._doc.save(self._path, incremental=True,
                               encryption=fitz.PDF_ENCRYPT_KEEP)
        self.set_dirty(False)

    def save_as(self, path: str, bake_fn=None) -> None:
        # Note: does not emit documentChanged — the in-memory document and any
        # live object layer are unchanged; only the target path moves.
        with QMutexLocker(self.lock):
            if bake_fn is not None:
                self._save_clone_with(path, bake_fn)
            else:
                self._doc.save(path, garbage=4, deflate=True)
        self._path = path
        self.set_dirty(False)
