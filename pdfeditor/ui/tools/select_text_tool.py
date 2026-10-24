"""Select-text tool.

Word-based linear text selection: drag from one word to another and every word
in reading order between them is selected. Double-click selects a single word.
The selected text can be copied to the clipboard.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

import fitz
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent

from .base_tool import Tool
from ..page_item import PageItem

# a "word" tuple from PyMuPDF: (x0, y0, x1, y1, text, block, line, word_no)
Word = Tuple[float, float, float, float, str, int, int, int]


class SelectTextTool(Tool):
    name = "select_text"
    cursor = Qt.CursorShape.IBeamCursor

    def __init__(self, context) -> None:
        super().__init__(context)
        self._anchor_item: Optional[PageItem] = None
        self._anchor_idx: Optional[int] = None
        self._words: List[Word] = []
        self._selected_text: str = ""

    # ---- helpers ---------------------------------------------------------
    def _load_words(self, item: PageItem) -> List[Word]:
        return self.ctx.view.document().page_words(item.page_index)

    @staticmethod
    def _nearest_word_index(words: List[Word], pt: fitz.Point) -> Optional[int]:
        """Index of the word whose rect contains pt, else the closest by centre."""
        best_i, best_d = None, None
        for i, w in enumerate(words):
            r = fitz.Rect(w[0], w[1], w[2], w[3])
            if r.contains(pt):
                return i
            cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
            d = (cx - pt.x) ** 2 + (cy - pt.y) ** 2
            if best_d is None or d < best_d:
                best_i, best_d = i, d
        return best_i

    def _apply_range(self, item: PageItem, i0: int, i1: int) -> None:
        lo, hi = sorted((i0, i1))
        chosen = self._words[lo:hi + 1]
        rects = [fitz.Rect(w[0], w[1], w[2], w[3]) for w in chosen]
        item.set_selection_rects(rects)
        self._selected_text = " ".join(w[4] for w in chosen)

    def _clear(self) -> None:
        view = self.ctx.view
        for it in view.page_items():
            it.set_selection_rects([])
        self._selected_text = ""

    # ---- events ----------------------------------------------------------
    def mouse_press(self, event: QMouseEvent, scene_pos: QPointF) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        item = self.ctx.item_at(scene_pos)
        self._clear()
        if item is None:
            self._anchor_item = None
            return True
        self._anchor_item = item
        self._words = self._load_words(item)
        pt = self.ctx.to_pdf(item, scene_pos)
        self._anchor_idx = self._nearest_word_index(self._words, pt)
        return True

    def mouse_move(self, event: QMouseEvent, scene_pos: QPointF) -> bool:
        if self._anchor_item is None or self._anchor_idx is None:
            return False
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            return False
        pt = self.ctx.to_pdf(self._anchor_item, scene_pos)
        idx = self._nearest_word_index(self._words, pt)
        if idx is not None:
            self._apply_range(self._anchor_item, self._anchor_idx, idx)
        return True

    def mouse_release(self, event: QMouseEvent, scene_pos: QPointF) -> bool:
        return self._anchor_item is not None

    def mouse_double_click(self, event: QMouseEvent, scene_pos: QPointF) -> bool:
        item = self.ctx.item_at(scene_pos)
        if item is None:
            return False
        self._clear()
        self._anchor_item = item
        self._words = self._load_words(item)
        pt = self.ctx.to_pdf(item, scene_pos)
        idx = self._nearest_word_index(self._words, pt)
        if idx is not None:
            self._anchor_idx = idx
            self._apply_range(item, idx, idx)
        return True

    # ---- public ----------------------------------------------------------
    def selected_text(self) -> str:
        return self._selected_text
