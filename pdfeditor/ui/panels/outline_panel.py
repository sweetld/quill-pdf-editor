"""Bookmarks dock — the document outline (table of contents) as a tree."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

from ...document import PdfDocument


class OutlinePanel(QWidget):
    pageRequested = Signal(int)  # 0-based target page

    def __init__(self, document: PdfDocument, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._document = document
        self._tree = QTreeWidget(self)
        self._tree.setHeaderHidden(True)
        self._tree.itemClicked.connect(self._on_clicked)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._tree)

    def reload(self) -> None:
        self._tree.clear()
        if not self._document.is_open:
            return
        toc = self._document.get_toc()  # [level, title, page(1-based)]
        stack = [(0, self._tree.invisibleRootItem())]
        for level, title, page in toc:
            node = QTreeWidgetItem([title])
            node.setData(0, Qt.ItemDataRole.UserRole, max(0, page - 1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            parent = stack[-1][1] if stack else self._tree.invisibleRootItem()
            parent.addChild(node)
            stack.append((level, node))
        self._tree.expandToDepth(1)

    def has_outline(self) -> bool:
        return self._tree.topLevelItemCount() > 0

    def _on_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        page = item.data(0, Qt.ItemDataRole.UserRole)
        if page is not None:
            self.pageRequested.emit(int(page))
