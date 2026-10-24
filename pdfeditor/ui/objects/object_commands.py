"""Undo commands for the editable-object layer.

These operate purely on the ObjectManager + scene items (no PyMuPDF), so they
are cheap and exact. Objects only touch the PDF when baked on save.
"""
from __future__ import annotations

from typing import Dict

from PySide6.QtCore import QRectF
from PySide6.QtGui import QUndoCommand

from .editable_item import EditableItem, clone_props
from .object_manager import ObjectManager


class AddObjectCommand(QUndoCommand):
    def __init__(self, manager: ObjectManager, item: EditableItem,
                 label: str = "Add object") -> None:
        super().__init__(label)
        self._manager = manager
        self._item = item

    def redo(self) -> None:
        self._manager.add(self._item)
        self._item.setSelected(True)

    def undo(self) -> None:
        self._manager.remove(self._item)


class RemoveObjectsCommand(QUndoCommand):
    def __init__(self, manager: ObjectManager, items, label: str = "Delete object") -> None:
        super().__init__(label if len(items) == 1 else f"Delete {len(items)} objects")
        self._manager = manager
        self._items = list(items)

    def redo(self) -> None:
        for it in self._items:
            self._manager.remove(it)

    def undo(self) -> None:
        for it in self._items:
            self._manager.add(it)


class LiftObjectCommand(QUndoCommand):
    """Lift existing page content (text / image / drawing) into an editable object.

    redo: redact the original region and add the editable item.
    undo: remove the object and restore the page from a pre-edit snapshot.
    On save the object is baked back, so the content round-trips.
    """

    def __init__(self, document, manager: ObjectManager, index: int, rect,
                 item: EditableItem, label: str = "Edit existing content",
                 redact_images: bool = False, redact_graphics: bool = False) -> None:
        super().__init__(label)
        self._document = document
        self._manager = manager
        self._index = index
        self._rect = rect
        self._item = item
        self._redact_images = redact_images
        self._redact_graphics = redact_graphics
        self._before = document.page_snapshot(index)
        self._after = None

    def redo(self) -> None:
        if self._after is None:
            self._document.redact_region(self._index, self._rect,
                                         images=self._redact_images,
                                         graphics=self._redact_graphics)
            self._after = self._document.page_snapshot(self._index)
        else:
            self._document.replace_page(self._index, self._after)
        self._manager.add(self._item)
        self._item.setSelected(True)

    def undo(self) -> None:
        self._manager.remove(self._item)
        self._document.replace_page(self._index, self._before)


# Backwards-compatible alias
LiftTextCommand = LiftObjectCommand


class GeometryCommand(QUndoCommand):
    """Move/resize: restore the object's point-rectangle."""

    def __init__(self, item: EditableItem, old_rect: QRectF, new_rect: QRectF) -> None:
        super().__init__("Move / resize object")
        self._item = item
        self._old = QRectF(old_rect)
        self._new = QRectF(new_rect)
        self._first = True

    def redo(self) -> None:
        # Skip the first redo: the interactive drag already left the item at _new.
        if self._first:
            self._first = False
            return
        self._item.set_rect_pt(self._new)

    def undo(self) -> None:
        self._item.set_rect_pt(self._old)


class PropertyCommand(QUndoCommand):
    """Change one or more style/content properties on an object."""

    def __init__(self, item: EditableItem, old_props: Dict, new_props: Dict,
                 label: str = "Change properties") -> None:
        super().__init__(label)
        self._item = item
        self._old = clone_props(old_props)
        self._new = clone_props(new_props)
        self._first = True

    def redo(self) -> None:
        if self._first:
            self._first = False
            return
        self._item.props = clone_props(self._new)
        self._item.update()

    def undo(self) -> None:
        self._item.props = clone_props(self._old)
        self._item.update()
