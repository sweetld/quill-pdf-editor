"""Find bar — searches the document and drives highlight navigation.

Search runs page-by-page via PdfDocument.search_page (PyMuPDF search_for).
Results are cached per query so Next/Prev just walk the flattened match list.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import fitz
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QToolButton,
                               QWidget)

from ..document import PdfDocument


class SearchBar(QWidget):
    # emitted with {page_index: [rects]}, active_page, active_rect
    # (object, not dict — the map has int keys, which QVariantMap can't marshal)
    resultsChanged = Signal(object, int, object)
    gotoPage = Signal(int)
    closed = Signal()

    def __init__(self, document: PdfDocument, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._document = document
        self._matches: List[Tuple[int, fitz.Rect]] = []  # flattened (page, rect)
        self._per_page: Dict[int, List[fitz.Rect]] = {}
        self._active = -1
        self._query = ""

        self._edit = QLineEdit(self)
        self._edit.setPlaceholderText("Find in document…")
        self._edit.returnPressed.connect(self.find_next)
        self._edit.textChanged.connect(self._on_text_changed)

        self._label = QLabel("", self)
        prev_btn = QToolButton(self); prev_btn.setText("▲"); prev_btn.setToolTip("Previous (Shift+Enter)")
        next_btn = QToolButton(self); next_btn.setText("▼"); next_btn.setToolTip("Next (Enter)")
        close_btn = QToolButton(self); close_btn.setText("✕"); close_btn.setToolTip("Close (Esc)")
        prev_btn.clicked.connect(self.find_prev)
        next_btn.clicked.connect(self.find_next)
        close_btn.clicked.connect(self._close)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 3, 6, 3)
        layout.addWidget(QLabel("Find:"))
        layout.addWidget(self._edit, 1)
        layout.addWidget(self._label)
        layout.addWidget(prev_btn)
        layout.addWidget(next_btn)
        layout.addWidget(close_btn)

    def focus_input(self) -> None:
        self._edit.setFocus()
        self._edit.selectAll()

    # ---- search ----------------------------------------------------------
    def _rebuild(self) -> None:
        self._matches.clear()
        self._per_page.clear()
        self._active = -1
        query = self._edit.text()
        self._query = query
        if not query or not self._document.is_open:
            self._label.setText("")
            self.resultsChanged.emit({}, -1, None)
            return
        for i in range(self._document.page_count):
            rects = self._document.search_page(i, query)
            if rects:
                self._per_page[i] = rects
                for r in rects:
                    self._matches.append((i, r))
        if self._matches:
            self._label.setText(f"{len(self._matches)} matches")
        else:
            self._label.setText("No matches")
        self.resultsChanged.emit(self._per_page, -1, None)

    def _on_text_changed(self, _text: str) -> None:
        self._rebuild()

    def _select(self, index: int) -> None:
        if not self._matches:
            return
        self._active = index % len(self._matches)
        page, rect = self._matches[self._active]
        self._label.setText(f"{self._active + 1} of {len(self._matches)}")
        self.resultsChanged.emit(self._per_page, page, rect)
        self.gotoPage.emit(page)

    def find_next(self) -> None:
        if self._query != self._edit.text():
            self._rebuild()
        self._select(self._active + 1 if self._active >= 0 else 0)

    def find_prev(self) -> None:
        if self._query != self._edit.text():
            self._rebuild()
        self._select(self._active - 1 if self._active >= 0 else -1)

    def _close(self) -> None:
        self.resultsChanged.emit({}, -1, None)
        self.hide()
        self.closed.emit()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self._close()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) \
                and (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.find_prev()
        else:
            super().keyPressEvent(event)
