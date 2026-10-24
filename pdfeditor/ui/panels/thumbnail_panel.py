"""Pages dock — page thumbnails with navigation, multi-select, drag-reorder
and a context menu that drives the page-management commands.

Thumbnails render lazily on a background thread pool (reusing the document
mutex) so opening a large file stays responsive.
"""
from __future__ import annotations

from typing import List, Optional

import fitz
from PySide6.QtCore import (QMutexLocker, QObject, QRunnable, QSize, Qt,
                            QThreadPool, Signal)
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QListWidget, QListWidgetItem,
                               QMenu, QVBoxLayout, QWidget)

from ...document import PdfDocument

THUMB_W = 120  # target thumbnail width in px
_INDEX_ROLE = Qt.ItemDataRole.UserRole


class _ThumbSignals(QObject):
    ready = Signal(int, QPixmap)


class _ThumbTask(QRunnable):
    def __init__(self, document: PdfDocument, index: int, width: int,
                 signals: _ThumbSignals) -> None:
        super().__init__()
        self._document = document
        self._index = index
        self._width = width
        self._signals = signals

    def run(self) -> None:
        doc = self._document.doc
        if doc is None:
            return
        try:
            with QMutexLocker(self._document.lock):
                if self._document.doc is not doc or self._index >= doc.page_count:
                    return
                page = doc.load_page(self._index)
                scale = self._width / page.rect.width if page.rect.width else 1.0
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                image = QImage(pix.samples, pix.width, pix.height, pix.stride,
                               QImage.Format.Format_RGB888).copy()
        except Exception:
            return
        self._signals.ready.emit(self._index, QPixmap.fromImage(image))


class _ThumbList(QListWidget):
    """QListWidget that reports a page permutation after an internal drag-drop."""

    pagesReordered = Signal(list)  # new order as a list of original page indices

    def dropEvent(self, event) -> None:  # noqa: N802
        src_item = self.currentItem()
        if src_item is None:
            event.ignore()
            return
        src_row = self.row(src_item)
        drop_row = self.indexAt(event.position().toPoint()).row()
        if drop_row < 0:
            drop_row = self.count() - 1
        if drop_row == src_row:
            event.ignore()
            return
        perm = list(range(self.count()))
        perm.insert(drop_row, perm.pop(src_row))
        event.acceptProposedAction()
        # Don't mutate the view directly — the command + reload will rebuild it.
        self.pagesReordered.emit(perm)


class ThumbnailPanel(QWidget):
    pageActivated = Signal(int)          # click → navigate (0-based)
    pagesReordered = Signal(list)        # drag-drop → new order
    contextActionRequested = Signal(str, list)  # (action_id, selected_indices)

    def __init__(self, document: PdfDocument, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._document = document
        self._pool = QThreadPool.globalInstance()
        self._signals = _ThumbSignals()
        self._signals.ready.connect(self._on_thumb_ready)
        self._suppress = False

        self._list = _ThumbList(self)
        self._list.setViewMode(QListWidget.ViewMode.IconMode)
        self._list.setIconSize(QSize(THUMB_W, int(THUMB_W * 1.4)))
        self._list.setGridSize(QSize(THUMB_W + 24, int(THUMB_W * 1.4) + 30))
        self._list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self._list.setMovement(QListWidget.Movement.Snap)
        self._list.setSpacing(6)
        self._list.setUniformItemSizes(True)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._show_context_menu)
        self._list.itemClicked.connect(self._on_item_clicked)
        self._list.pagesReordered.connect(self.pagesReordered)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._list)

    # ---- population ------------------------------------------------------
    def reload(self) -> None:
        self._list.clear()
        if not self._document.is_open:
            return
        placeholder = QPixmap(THUMB_W, int(THUMB_W * 1.4))
        placeholder.fill(Qt.GlobalColor.white)
        icon = QIcon(placeholder)
        for i in range(self._document.page_count):
            item = QListWidgetItem(icon, str(i + 1))
            item.setData(_INDEX_ROLE, i)
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)
            self._list.addItem(item)
            self._pool.start(_ThumbTask(self._document, i, THUMB_W, self._signals))

    def refresh_page(self, index: int) -> None:
        if self._document.is_open and 0 <= index < self._list.count():
            self._pool.start(_ThumbTask(self._document, index, THUMB_W, self._signals))

    def set_current_page(self, index: int) -> None:
        if 0 <= index < self._list.count():
            self._suppress = True
            self._list.setCurrentRow(index)
            self._list.scrollToItem(self._list.item(index))
            self._suppress = False

    def selected_indices(self) -> List[int]:
        return sorted(self._list.row(i) for i in self._list.selectedItems())

    # ---- events ----------------------------------------------------------
    def _on_thumb_ready(self, index: int, pixmap: QPixmap) -> None:
        item = self._list.item(index)
        if item is not None:
            item.setIcon(QIcon(pixmap))

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        if not self._suppress:
            self.pageActivated.emit(self._list.row(item))

    def _show_context_menu(self, pos) -> None:
        if not self._document.is_open:
            return
        indices = self.selected_indices()
        if not indices:
            row = self._list.indexAt(pos).row()
            if row < 0:
                return
            indices = [row]
        menu = QMenu(self)
        many = len(indices) > 1
        actions = [
            ("insert_before", "Insert Blank Page Before"),
            ("insert_after", "Insert Blank Page After"),
            ("insert_file", "Insert Pages from File…"),
            (None, None),
            ("duplicate", "Duplicate Page"),
            ("rotate_left", "Rotate Left"),
            ("rotate_right", "Rotate Right"),
            (None, None),
            ("extract", "Extract Page(s)…" if many else "Extract Page…"),
            ("delete", "Delete Page(s)" if many else "Delete Page"),
        ]
        for action_id, label in actions:
            if action_id is None:
                menu.addSeparator()
                continue
            act = menu.addAction(label)
            act.triggered.connect(
                lambda _c=False, a=action_id: self.contextActionRequested.emit(a, indices))
        menu.exec(self._list.viewport().mapToGlobal(pos))
