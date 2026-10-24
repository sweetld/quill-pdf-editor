"""Properties dock — edits the style/content of the selected object.

Rebuilt whenever the scene selection changes. The whole body widget is
replaced on each rebuild (rather than trying to empty a nested layout), which
guarantees stale controls never linger. Each edit becomes a PropertyCommand on
the shared undo stack.
"""
from __future__ import annotations

from typing import Callable, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QDoubleSpinBox,
                               QFormLayout, QLabel, QPlainTextEdit,
                               QPushButton, QVBoxLayout, QWidget)

from ..objects.editable_item import EditableItem, clone_props


class _CommitTextEdit(QPlainTextEdit):
    """A plain-text editor that emits editingFinished when it loses focus."""

    editingFinished = Signal()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.editingFinished.emit()


class _ColorButton(QPushButton):
    def __init__(self, color: Optional[QColor], on_pick: Callable) -> None:
        super().__init__()
        self._on_pick = on_pick
        self.setFixedHeight(26)
        self.set_color(color)
        self.clicked.connect(self._pick)

    def set_color(self, color: Optional[QColor]) -> None:
        self._color = color
        if color is None or not color.isValid():
            self.setText("(none)")
            self.setStyleSheet("")
        else:
            self.setText(color.name())
            fg = "#000" if color.lightnessF() > 0.5 else "#fff"
            self.setStyleSheet(f"background:{color.name()}; color:{fg};")

    def _pick(self) -> None:
        initial = self._color if self._color and self._color.isValid() else QColor(0, 0, 0)
        color = QColorDialog.getColor(initial, self, "Choose colour")
        if color.isValid():
            self._on_pick(color)


class PropertiesPanel(QWidget):
    def __init__(self, push: Callable, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._push = push
        self._item: Optional[EditableItem] = None
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(0, 0, 0, 0)
        self._body: Optional[QWidget] = None
        self._rebuild()

    # ---- selection -------------------------------------------------------
    def set_selection(self, items: List[EditableItem]) -> None:
        self._item = items[0] if len(items) == 1 else None
        self._rebuild()

    def _new_body(self) -> QVBoxLayout:
        if self._body is not None:
            self._outer.removeWidget(self._body)
            self._body.setParent(None)
            self._body.deleteLater()
        self._body = QWidget(self)
        self._outer.addWidget(self._body)
        layout = QVBoxLayout(self._body)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        return layout

    def _rebuild(self) -> None:
        layout = self._new_body()
        if self._item is None:
            hint = QLabel("Select a single object to edit its properties.")
            hint.setWordWrap(True)
            hint.setStyleSheet("color: gray;")
            layout.addWidget(hint)
            return
        layout.addWidget(QLabel(f"<b>{self._item.obj_type.capitalize()}</b>"))
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        layout.addLayout(form)
        t = self._item.obj_type
        if t == "text":
            self._build_text(form)
        elif t in ("rect", "ellipse"):
            self._build_shape(form, fill=True)
        elif t == "line":
            self._build_shape(form, fill=False)
        elif t == "image":
            form.addRow(QLabel("Drag the handles to resize."))

    # ---- commit ----------------------------------------------------------
    def _commit(self, **changes) -> None:
        if self._item is None:
            return
        old = clone_props(self._item.props)
        new = clone_props(self._item.props)
        new.update(changes)
        if new == old:
            return
        self._item.props = new
        self._item.update()
        from ..objects.object_commands import PropertyCommand
        self._push(PropertyCommand(self._item, old, new))

    # ---- builders --------------------------------------------------------
    def _build_text(self, form: QFormLayout) -> None:
        editor = _CommitTextEdit()
        editor.setPlainText(self._item.props.get("text", ""))
        editor.setFixedHeight(90)
        editor.editingFinished.connect(
            lambda e=editor: self._commit(text=e.toPlainText()))
        form.addRow("Text", editor)

        size = QDoubleSpinBox()
        size.setRange(4, 400)
        size.setValue(self._item.props.get("fontsize", 12.0))
        size.editingFinished.connect(lambda s=size: self._commit(fontsize=s.value()))
        form.addRow("Font size", size)

        bold = QCheckBox("Bold")
        bold.setChecked(bool(self._item.props.get("bold", False)))
        bold.toggled.connect(lambda v: self._commit(bold=v))
        italic = QCheckBox("Italic")
        italic.setChecked(bool(self._item.props.get("italic", False)))
        italic.toggled.connect(lambda v: self._commit(italic=v))
        form.addRow("Bold", bold)
        form.addRow("Italic", italic)

        form.addRow("Colour", _ColorButton(self._item.props.get("color"),
                                            lambda c: self._commit(color=c)))

    def _build_shape(self, form: QFormLayout, fill: bool) -> None:
        form.addRow("Stroke", _ColorButton(self._item.props.get("stroke"),
                                            lambda c: self._commit(stroke=c)))
        width = QDoubleSpinBox()
        width.setRange(0.0, 50.0)
        width.setSingleStep(0.5)
        width.setValue(self._item.props.get("width", 1.0))
        width.editingFinished.connect(lambda s=width: self._commit(width=s.value()))
        form.addRow("Line width", width)

        if fill:
            has_fill = self._item.props.get("fill") is not None
            fill_check = QCheckBox("Filled")
            fill_check.setChecked(has_fill)
            fill_btn = _ColorButton(self._item.props.get("fill") or QColor(255, 255, 255),
                                    lambda c: self._commit(fill=c))
            fill_btn.setEnabled(has_fill)

            def on_toggle(checked, btn=fill_btn):
                btn.setEnabled(checked)
                self._commit(fill=(QColor(255, 255, 255) if checked else None))
            fill_check.toggled.connect(on_toggle)
            form.addRow("Fill", fill_check)
            form.addRow("Fill colour", fill_btn)
