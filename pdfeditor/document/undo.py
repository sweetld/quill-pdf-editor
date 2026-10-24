"""QUndoCommand subclasses for page-management operations.

PyMuPDF has no native undo, so each command records exactly what it needs to
reverse itself:

  * rotation → the previous angle per page
  * reorder  → the inverse permutation
  * delete   → an in-memory copy of each removed page, reinserted on undo
  * insert / duplicate → the inverse is a delete of the inserted range

The document's own primitives (PdfDocument.*) do the real work and emit
``structureChanged`` so the views refresh after both redo() and undo().
"""
from __future__ import annotations

from typing import Dict, List, Optional

import fitz
from PySide6.QtGui import QUndoCommand

from .pdf_document import PdfDocument


class RotateCommand(QUndoCommand):
    def __init__(self, document: PdfDocument, indices: List[int], delta: int) -> None:
        label = "Rotate page" if len(indices) == 1 else f"Rotate {len(indices)} pages"
        super().__init__(label)
        self._document = document
        self._indices = list(indices)
        self._delta = delta
        self._old: Dict[int, int] = {i: document.rotation(i) for i in indices}

    def redo(self) -> None:
        for i in self._indices:
            self._document.set_rotation(i, (self._old[i] + self._delta) % 360)

    def undo(self) -> None:
        for i in self._indices:
            self._document.set_rotation(i, self._old[i])


class ReorderCommand(QUndoCommand):
    """``perm`` is the new order expressed as current page indices."""

    def __init__(self, document: PdfDocument, perm: List[int],
                 label: str = "Reorder pages") -> None:
        super().__init__(label)
        self._document = document
        self._perm = list(perm)
        self._inverse = [0] * len(perm)
        for new_pos, old_index in enumerate(perm):
            self._inverse[old_index] = new_pos

    def redo(self) -> None:
        self._document.reorder(self._perm)

    def undo(self) -> None:
        self._document.reorder(self._inverse)


class MovePageCommand(ReorderCommand):
    def __init__(self, document: PdfDocument, src: int, dst: int) -> None:
        n = document.page_count
        perm = list(range(n))
        val = perm.pop(src)
        perm.insert(dst, val)
        super().__init__(document, perm, "Move page")


class DeletePagesCommand(QUndoCommand):
    def __init__(self, document: PdfDocument, indices: List[int]) -> None:
        label = "Delete page" if len(indices) == 1 else f"Delete {len(indices)} pages"
        super().__init__(label)
        self._document = document
        self._indices = sorted(indices)
        self._saved: List[tuple] = []  # (original_index, single-page fitz.Document)

    def redo(self) -> None:
        # Snapshot each page before removing (copies survive on the undo stack).
        self._saved = [(idx, self._document.extract_page_doc(idx))
                       for idx in self._indices]
        self._document.delete_pages(self._indices)

    def undo(self) -> None:
        # Reinsert ascending so each original index lands in the right slot.
        for idx, sub in self._saved:
            self._document.insert_doc(sub, at=idx)


class InsertBlankCommand(QUndoCommand):
    def __init__(self, document: PdfDocument, at: int,
                 width: float, height: float) -> None:
        super().__init__("Insert blank page")
        self._document = document
        self._at = at
        self._width = width
        self._height = height

    def redo(self) -> None:
        self._document.insert_blank(self._at, self._width, self._height)

    def undo(self) -> None:
        self._document.delete_pages([self._at])


class DuplicatePageCommand(QUndoCommand):
    def __init__(self, document: PdfDocument, index: int) -> None:
        super().__init__("Duplicate page")
        self._document = document
        self._index = index

    def redo(self) -> None:
        self._document.duplicate_page(self._index)

    def undo(self) -> None:
        self._document.delete_pages([self._index + 1])


class EditTextCommand(QUndoCommand):
    """Edit existing page text via redaction + reinsertion.

    Undo/redo swap the whole page for a snapshot taken before / after the edit,
    which makes the imperfect text operation perfectly reversible.
    """

    def __init__(self, document: PdfDocument, index: int, rect: "fitz.Rect",
                 new_text: str, fontsize: float, color, fontname: str = "helv") -> None:
        super().__init__("Edit text")
        self._document = document
        self._index = index
        self._rect = rect
        self._new_text = new_text
        self._fontsize = fontsize
        self._color = color
        self._fontname = fontname
        self._before = document.page_snapshot(index)
        self._after: Optional[fitz.Document] = None

    def redo(self) -> None:
        if self._after is None:
            self._document.replace_text_block(
                self._index, self._rect, self._new_text, self._fontsize, self._color,
                fontname=self._fontname)
            self._after = self._document.page_snapshot(self._index)
        else:
            self._document.replace_page(self._index, self._after)

    def undo(self) -> None:
        self._document.replace_page(self._index, self._before)


class InsertFromFileCommand(QUndoCommand):
    def __init__(self, document: PdfDocument, src_path: str, at: int) -> None:
        super().__init__("Insert pages from file")
        self._document = document
        self._at = at
        self._src: Optional[fitz.Document] = fitz.open(src_path)
        self._count = self._src.page_count

    def redo(self) -> None:
        self._document.insert_doc(self._src, at=self._at)

    def undo(self) -> None:
        self._document.delete_pages(list(range(self._at, self._at + self._count)))
