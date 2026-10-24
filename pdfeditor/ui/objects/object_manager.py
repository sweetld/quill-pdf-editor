"""ObjectManager — owns the editable objects for the current document.

Objects are parented to their page's PageItem in the scene. The manager keeps a
flat list for baking/serialisation, rescales objects when the zoom changes, and
re-parents them after the view rebuilds page items (e.g. after a relayout).
"""
from __future__ import annotations

from typing import Callable, List, Optional

import fitz
from PySide6.QtCore import QMutexLocker, QObject, QRectF, Signal

from .editable_item import EditableItem


class ObjectManager(QObject):
    objectChanged = Signal(object, QRectF, QRectF)  # bubbled from items (for undo)

    def __init__(self, view, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._view = view
        self._items: List[EditableItem] = []
        view.zoomChanged.connect(self._on_zoom_changed)

    def items(self) -> List[EditableItem]:
        return list(self._items)

    def items_on_page(self, page_index: int) -> List[EditableItem]:
        return [it for it in self._items if it.page_index == page_index]

    def has_objects(self) -> bool:
        return bool(self._items)

    def clear(self) -> None:
        for it in self._items:
            if it.scene() is not None:
                it.scene().removeItem(it)
        self._items.clear()

    # ---- add / remove (called by undo commands) -------------------------
    def add(self, item: EditableItem) -> None:
        page_item = self._view.page_item_for(item.page_index)
        if page_item is not None:
            item.setParentItem(page_item)
        item.set_zoom(self._view.zoom())
        item.changed.connect(self.objectChanged)
        self._items.append(item)

    def remove(self, item: EditableItem) -> None:
        if item in self._items:
            self._items.remove(item)
        if item.scene() is not None:
            item.scene().removeItem(item)
        item.setParentItem(None)

    # ---- zoom / relayout -------------------------------------------------
    def _on_zoom_changed(self, zoom: float) -> None:
        for it in self._items:
            it.set_zoom(zoom)

    def reattach(self) -> None:
        """Re-parent items to freshly created PageItems and rescale them."""
        zoom = self._view.zoom()
        for it in self._items:
            page_item = self._view.page_item_for(it.page_index)
            if page_item is not None:
                it.setParentItem(page_item)
                it.set_zoom(zoom)

    # ---- baking ----------------------------------------------------------
    def bake_into(self, document, clear: bool = False) -> None:
        """Write every object into its page of ``document`` (a PdfDocument)."""
        doc = document.doc
        if doc is None:
            return
        with QMutexLocker(document.lock):
            for it in self._items:
                if 0 <= it.page_index < doc.page_count:
                    it.bake(doc.load_page(it.page_index))
        if clear:
            self.clear()
