"""Edit-existing-text tool.

Click a text block to edit its content. On commit the original text under the
block is redacted away and the new text reinserted in the same rectangle.

This is the inherently best-effort part of PDF editing: PDFs store positioned
glyphs, not paragraphs, so complex layouts (mixed fonts, tight leading, colour)
may not reproduce exactly. Simple text runs edit cleanly.
"""
from __future__ import annotations

from typing import Callable, Optional

import fitz
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QMouseEvent
from PySide6.QtWidgets import QInputDialog

from .base_tool import Tool


class EditTextTool(Tool):
    name = "edit_text"
    cursor = Qt.CursorShape.IBeamCursor

    def __init__(self, context, push: Callable, make_command: Callable) -> None:
        super().__init__(context)
        self._push = push
        self._make_command = make_command  # (index, rect, text, size, color) -> QUndoCommand

    def mouse_press(self, event: QMouseEvent, scene_pos: QPointF) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        page = self.ctx.item_at(scene_pos)
        if page is None:
            return True
        pt = self.ctx.to_pdf(page, scene_pos)
        doc = self.ctx.view.document()
        info = doc.text_block_at(page.page_index, pt)
        if info is None:
            return True
        rect, text, size, color_int, flags, fontname = info
        current = text.rstrip("\n")
        new_text, ok = QInputDialog.getMultiLineText(
            self.ctx.view, "Edit Text", "Edit the text of this block:", current)
        if ok and new_text != current:
            from ...fonts import base14_alias, style_from_flags
            style = style_from_flags(flags, fontname)
            alias = base14_alias(style["bold"], style["italic"],
                                 style["serif"], style["mono"])
            color = self._fitz_color(color_int)
            command = self._make_command(page.page_index, rect, new_text, size,
                                         color, alias)
            self._push(command)
        return True

    @staticmethod
    def _fitz_color(color_int: int):
        try:
            r, g, b = fitz.sRGB_to_rgb(color_int)
        except Exception:
            r = (color_int >> 16) & 255
            g = (color_int >> 8) & 255
            b = color_int & 255
        return (r / 255.0, g / 255.0, b / 255.0)
