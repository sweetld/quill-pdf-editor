"""Tool strategy interface.

The active tool receives mouse/key events forwarded by the DocumentView, along
with a ToolContext that lets it resolve which PageItem an event landed on and
translate scene coordinates into PDF-point coordinates.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

import fitz
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent

from ..page_item import PageItem


class ViewLike(Protocol):
    def page_item_at(self, scene_pos: QPointF) -> Optional[PageItem]: ...
    def document(self): ...


@dataclass
class ToolContext:
    view: ViewLike

    def item_at(self, scene_pos: QPointF) -> Optional[PageItem]:
        return self.view.page_item_at(scene_pos)

    def to_pdf(self, item: PageItem, scene_pos: QPointF) -> fitz.Point:
        local = item.mapFromScene(scene_pos)
        return item.to_pdf_point(local.x(), local.y())


class Tool:
    """Base class; override the handlers you care about.

    Return ``True`` from a handler to indicate the event was consumed (the view
    then skips its own default handling, e.g. rubber-band panning)."""

    name: str = "tool"
    cursor: Qt.CursorShape = Qt.CursorShape.ArrowCursor

    def __init__(self, context: ToolContext) -> None:
        self.ctx = context

    def activate(self) -> None:  # noqa: D401 - hook
        pass

    def deactivate(self) -> None:
        pass

    def mouse_press(self, event: QMouseEvent, scene_pos: QPointF) -> bool:
        return False

    def mouse_move(self, event: QMouseEvent, scene_pos: QPointF) -> bool:
        return False

    def mouse_release(self, event: QMouseEvent, scene_pos: QPointF) -> bool:
        return False

    def mouse_double_click(self, event: QMouseEvent, scene_pos: QPointF) -> bool:
        return False
