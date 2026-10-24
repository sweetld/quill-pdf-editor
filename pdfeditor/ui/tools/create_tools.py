"""Creation tools — drag on the page to add a text box, shape, or image.

Each tool draws a live rubber-band preview while dragging, then on release
builds the matching EditableItem and pushes an AddObjectCommand so creation is
undoable. A tiny drag is treated as a click and produces a default-sized object.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QMouseEvent, QPen
from PySide6.QtWidgets import QGraphicsRectItem

from .base_tool import Tool
from ..objects.editable_item import make_item
from ..objects.object_commands import AddObjectCommand
from ..page_item import PageItem

DEFAULT_TEXT_SIZE = (180.0, 40.0)   # points
DEFAULT_SHAPE_SIZE = (120.0, 90.0)


class CreateTool(Tool):
    cursor = Qt.CursorShape.CrossCursor
    default_label = "Add object"

    def __init__(self, context, manager, push: Callable) -> None:
        super().__init__(context)
        self._manager = manager
        self._push = push
        self._page: Optional[PageItem] = None
        self._start_pt: Optional[QPointF] = None
        self._preview: Optional[QGraphicsRectItem] = None

    # subclasses build the object from a points-rectangle on a page
    def make_object(self, page_index: int, rect_pt: QRectF):
        raise NotImplementedError

    def default_size(self):
        return DEFAULT_SHAPE_SIZE

    def _pdf_point(self, page: PageItem, scene_pos: QPointF) -> QPointF:
        local = page.mapFromScene(scene_pos)
        p = page.to_pdf_point(local.x(), local.y())
        return QPointF(p.x, p.y)

    def mouse_press(self, event: QMouseEvent, scene_pos: QPointF) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        page = self.ctx.item_at(scene_pos)
        if page is None:
            return True
        self._page = page
        self._start_pt = self._pdf_point(page, scene_pos)
        self._preview = QGraphicsRectItem(page)
        self._preview.setPen(QPen(QColor(40, 110, 220), 0, Qt.PenStyle.DashLine))
        self._preview.setRect(QRectF())
        return True

    def mouse_move(self, event: QMouseEvent, scene_pos: QPointF) -> bool:
        if self._page is None or self._preview is None:
            return False
        cur = self._pdf_point(self._page, scene_pos)
        z = self._page.zoom
        r = QRectF(self._start_pt, cur).normalized()
        self._preview.setRect(QRectF(r.left() * z, r.top() * z,
                                     r.width() * z, r.height() * z))
        return True

    def mouse_release(self, event: QMouseEvent, scene_pos: QPointF) -> bool:
        if self._page is None:
            return False
        end = self._pdf_point(self._page, scene_pos)
        rect = QRectF(self._start_pt, end).normalized()
        if rect.width() < 8 or rect.height() < 8:
            dw, dh = self.default_size()
            rect = QRectF(self._start_pt, QPointF(self._start_pt.x() + dw,
                                                  self._start_pt.y() + dh))
        if self._preview is not None and self._preview.scene() is not None:
            self._preview.scene().removeItem(self._preview)
        item = self.make_object(self._page.page_index, rect)
        self._page = None
        self._start_pt = None
        self._preview = None
        if item is not None:
            self._push(AddObjectCommand(self._manager, item, self.default_label))
            self.after_create(item)
        return True

    def after_create(self, item) -> None:  # hook for subclasses
        pass


class InsertTextTool(CreateTool):
    name = "insert_text"
    default_label = "Add text box"

    def __init__(self, context, manager, push, edit_text=None) -> None:
        super().__init__(context, manager, push)
        self._edit_text = edit_text

    def default_size(self):
        return DEFAULT_TEXT_SIZE

    def make_object(self, page_index, rect_pt):
        return make_item("text", page_index, rect_pt, {
            "text": "", "fontsize": 12.0, "color": QColor(0, 0, 0),
            "fontfamily": "Helvetica"})

    def after_create(self, item) -> None:
        # Immediately prompt for the text so the box isn't left empty.
        if self._edit_text is not None:
            self._edit_text(item)


class ShapeTool(CreateTool):
    def __init__(self, context, manager, push, shape: str) -> None:
        super().__init__(context, manager, push)
        self._shape = shape
        self.name = f"shape_{shape}"
        self.default_label = f"Add {shape}"

    def make_object(self, page_index, rect_pt):
        props = {"stroke": QColor(0, 0, 0), "fill": None, "width": 1.5}
        return make_item(self._shape, page_index, rect_pt, props)


class InsertImageTool(CreateTool):
    name = "insert_image"
    default_label = "Insert image"

    def __init__(self, context, manager, push, pick_image: Callable) -> None:
        super().__init__(context, manager, push)
        self._pick_image = pick_image
        self._pending: Optional[bytes] = None

    def activate(self) -> None:
        self._pending = self._pick_image()

    def make_object(self, page_index, rect_pt):
        if not self._pending:
            return None
        item = make_item("image", page_index, rect_pt, {"image_bytes": self._pending})
        return item
