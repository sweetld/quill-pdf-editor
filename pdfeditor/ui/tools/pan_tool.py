"""Hand/pan tool — drag to scroll the document view."""
from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent

from .base_tool import Tool


class PanTool(Tool):
    name = "pan"
    cursor = Qt.CursorShape.OpenHandCursor

    def activate(self) -> None:
        # Let QGraphicsView do the scroll-hand dragging natively.
        self.ctx.view.setDragMode(self.ctx.view.DragMode.ScrollHandDrag)

    def deactivate(self) -> None:
        self.ctx.view.setDragMode(self.ctx.view.DragMode.NoDrag)

    def mouse_press(self, event: QMouseEvent, scene_pos: QPointF) -> bool:
        # Defer to the view's built-in ScrollHandDrag.
        return False
