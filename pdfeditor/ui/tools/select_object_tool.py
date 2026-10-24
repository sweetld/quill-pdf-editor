"""Select/move/resize objects.

This tool defers to Qt's own item interaction: it puts the view in
RubberBandDrag mode (band-select on empty canvas; click-drag to move an item;
grab a handle to resize) and consumes nothing itself.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent

from .base_tool import Tool
from ..objects.editable_item import EditableItem, TextItem


class SelectObjectTool(Tool):
    name = "select_object"
    cursor = Qt.CursorShape.ArrowCursor

    def __init__(self, context, edit_text: Optional[Callable] = None,
                 lift: Optional[Callable] = None) -> None:
        super().__init__(context)
        self._edit_text = edit_text
        self._lift = lift

    def activate(self) -> None:
        self.ctx.view.setDragMode(self.ctx.view.DragMode.RubberBandDrag)
        for it in self.ctx.view.object_manager().items():
            it.setFlag(it.GraphicsItemFlag.ItemIsSelectable, True)
            it.setFlag(it.GraphicsItemFlag.ItemIsMovable, True)

    def deactivate(self) -> None:
        self.ctx.view.setDragMode(self.ctx.view.DragMode.NoDrag)
        self.ctx.view.scene().clearSelection()

    def mouse_press(self, event: QMouseEvent, scene_pos: QPointF) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        view = self.ctx.view
        # If the click landed on an existing overlay object (or its handles),
        # let QGraphicsView handle selection / move / resize.
        node = view.scene().itemAt(scene_pos, view.transform())
        while node is not None:
            if isinstance(node, EditableItem):
                return False
            node = node.parentItem()
        # Otherwise try to lift underlying page content into an editable object.
        page = self.ctx.item_at(scene_pos)
        if page is not None and self._lift is not None:
            pt = self.ctx.to_pdf(page, scene_pos)
            if self._lift(page.page_index, pt):
                return True
        return False  # nothing under cursor: allow rubber-band selection

    def mouse_double_click(self, event: QMouseEvent, scene_pos: QPointF) -> bool:
        view = self.ctx.view
        item = view.scene().itemAt(scene_pos, view.transform())
        if isinstance(item, TextItem) and self._edit_text is not None:
            self._edit_text(item)
            return True
        return False
