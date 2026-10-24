"""Editable page objects — the interactive content-editing layer.

Each object is a QGraphicsObject parented to its page's PageItem, so it moves
with the page automatically. Geometry is stored in **PDF points**; the item
rescales to pixels for the current zoom. Items support selection, dragging,
8-handle resize (2 handles for lines), and know how to bake themselves into a
PyMuPDF page on save.

Colours are held as QColor (or None for "no fill") and converted to fitz's
0..1 RGB tuples at bake time.
"""
from __future__ import annotations

from typing import Dict, Optional

import fitz
from PySide6.QtCore import QPointF, QRectF, QSizeF, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QImage, QPainter,
                           QPainterPath, QPen, QPixmap, QPolygonF)
from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject

HANDLE = 5.0        # half-size of a resize handle, in px
MIN_SIZE_PT = 6.0   # smallest object dimension, in points

_CORNERS = ("nw", "n", "ne", "e", "se", "s", "sw", "w")


def qcolor_to_fitz(color: Optional[QColor]):
    if color is None or not color.isValid():
        return None
    return (color.redF(), color.greenF(), color.blueF())


def clone_props(props: Dict) -> Dict:
    """Copy a props dict, duplicating Qt value objects (which don't deepcopy)."""
    out: Dict = {}
    for key, value in props.items():
        if isinstance(value, QColor):
            out[key] = QColor(value)
        elif isinstance(value, QPointF):
            out[key] = QPointF(value)
        else:
            out[key] = value
    return out


class EditableItem(QGraphicsObject):
    """Base class for rect/ellipse/text/image objects (rect-based geometry)."""

    changed = Signal(object, QRectF, QRectF)  # item, old_rect_pt, new_rect_pt

    obj_type = "object"

    def __init__(self, page_index: int, rect_pt: QRectF,
                 props: Optional[Dict] = None) -> None:
        super().__init__()
        self.page_index = page_index
        self._rect_pt = QRectF(rect_pt).normalized()
        self.props: Dict = props.copy() if props else {}
        self._zoom = 1.0
        self._w = 0.0
        self._h = 0.0
        self._resize_handle: Optional[str] = None
        self._press_rect: Optional[QRectF] = None
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
                      | QGraphicsItem.GraphicsItemFlag.ItemIsMovable
                      | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.setAcceptHoverEvents(True)

    # ---- geometry --------------------------------------------------------
    def set_zoom(self, zoom: float) -> None:
        self._zoom = zoom
        self._apply()

    def _apply(self) -> None:
        self.prepareGeometryChange()
        z = self._zoom
        r = self._rect_pt
        self.setPos(r.left() * z, r.top() * z)
        self._w = max(1.0, r.width() * z)
        self._h = max(1.0, r.height() * z)
        self.update()

    def rect_pt(self) -> QRectF:
        return QRectF(self._rect_pt)

    def set_rect_pt(self, rect_pt: QRectF) -> None:
        self._rect_pt = QRectF(rect_pt).normalized()
        self._apply()

    def snapshot(self) -> Dict:
        return {"rect": QRectF(self._rect_pt), "props": clone_props(self.props)}

    def restore(self, snap: Dict) -> None:
        self.props = clone_props(snap["props"])
        self.set_rect_pt(snap["rect"])

    def boundingRect(self) -> QRectF:
        return QRectF(-HANDLE, -HANDLE, self._w + 2 * HANDLE, self._h + 2 * HANDLE)

    def content_rect(self) -> QRectF:
        return QRectF(0, 0, self._w, self._h)

    # ---- handles ---------------------------------------------------------
    def _handle_points(self) -> Dict[str, QPointF]:
        w, h = self._w, self._h
        return {
            "nw": QPointF(0, 0), "n": QPointF(w / 2, 0), "ne": QPointF(w, 0),
            "e": QPointF(w, h / 2), "se": QPointF(w, h), "s": QPointF(w / 2, h),
            "sw": QPointF(0, h), "w": QPointF(0, h / 2),
        }

    def _handle_at(self, pos: QPointF) -> Optional[str]:
        for hid, pt in self._handle_points().items():
            if abs(pos.x() - pt.x()) <= HANDLE + 1 and abs(pos.y() - pt.y()) <= HANDLE + 1:
                return hid
        return None

    # ---- painting --------------------------------------------------------
    def paint(self, painter: QPainter, option, widget=None) -> None:
        self.paint_content(painter)
        if self.isSelected():
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            pen = QPen(QColor(40, 110, 220), 1, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(self.content_rect())
            painter.setPen(QPen(QColor(40, 110, 220), 1))
            painter.setBrush(QBrush(QColor(255, 255, 255)))
            for pt in self._handle_points().values():
                painter.drawRect(QRectF(pt.x() - HANDLE, pt.y() - HANDLE,
                                        2 * HANDLE, 2 * HANDLE))

    def paint_content(self, painter: QPainter) -> None:  # overridden
        pass

    def bake(self, page: "fitz.Page") -> None:  # overridden
        raise NotImplementedError

    # ---- mouse: move / resize -------------------------------------------
    def mousePressEvent(self, event) -> None:
        self._press_rect = self.rect_pt()
        handle = self._handle_at(event.pos()) if self.isSelected() else None
        self._resize_handle = handle
        if handle is not None:
            self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._resize_handle is not None:
            self._resize_to(self.mapToParent(event.pos()))
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        if self._resize_handle is None:
            # a plain drag moved our origin; fold that back into rect_pt
            z = self._zoom or 1.0
            tl = QPointF(self.pos().x() / z, self.pos().y() / z)
            self._rect_pt = QRectF(tl, QSizeF(self._rect_pt.width(),
                                              self._rect_pt.height()))
        self._resize_handle = None
        if self._press_rect is not None and self._press_rect != self._rect_pt:
            self.changed.emit(self, self._press_rect, QRectF(self._rect_pt))
        self._press_rect = None

    def _resize_to(self, parent_pos: QPointF) -> None:
        z = self._zoom or 1.0
        px, py = parent_pos.x() / z, parent_pos.y() / z
        r = QRectF(self._rect_pt)
        left, top, right, bottom = r.left(), r.top(), r.right(), r.bottom()
        h = self._resize_handle
        if "w" in h:
            left = min(px, right - MIN_SIZE_PT)
        if "e" in h:
            right = max(px, left + MIN_SIZE_PT)
        if "n" in h:
            top = min(py, bottom - MIN_SIZE_PT)
        if "s" in h:
            bottom = max(py, top + MIN_SIZE_PT)
        self.set_rect_pt(QRectF(QPointF(left, top), QPointF(right, bottom)))


class RectItem(EditableItem):
    obj_type = "rect"

    def paint_content(self, painter: QPainter) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        stroke = self.props.get("stroke")
        fill = self.props.get("fill")
        width = self.props.get("width", 1.0) * self._zoom
        painter.setPen(QPen(stroke, width) if stroke else QPen(Qt.PenStyle.NoPen))
        painter.setBrush(QBrush(fill) if fill else QBrush(Qt.BrushStyle.NoBrush))
        painter.drawRect(self.content_rect())

    def bake(self, page: "fitz.Page") -> None:
        r = self._rect_pt
        page.draw_rect(fitz.Rect(r.left(), r.top(), r.right(), r.bottom()),
                       color=qcolor_to_fitz(self.props.get("stroke")),
                       fill=qcolor_to_fitz(self.props.get("fill")),
                       width=self.props.get("width", 1.0))


class EllipseItem(EditableItem):
    obj_type = "ellipse"

    def paint_content(self, painter: QPainter) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        stroke = self.props.get("stroke")
        fill = self.props.get("fill")
        width = self.props.get("width", 1.0) * self._zoom
        painter.setPen(QPen(stroke, width) if stroke else QPen(Qt.PenStyle.NoPen))
        painter.setBrush(QBrush(fill) if fill else QBrush(Qt.BrushStyle.NoBrush))
        painter.drawEllipse(self.content_rect())

    def bake(self, page: "fitz.Page") -> None:
        r = self._rect_pt
        page.draw_oval(fitz.Rect(r.left(), r.top(), r.right(), r.bottom()),
                       color=qcolor_to_fitz(self.props.get("stroke")),
                       fill=qcolor_to_fitz(self.props.get("fill")),
                       width=self.props.get("width", 1.0))


class LineItem(EditableItem):
    """A line stored as two endpoints in points (props['p1'], props['p2'])."""

    obj_type = "line"

    def __init__(self, page_index, rect_pt, props=None) -> None:
        super().__init__(page_index, rect_pt, props)
        self.props.setdefault("p1", QPointF(self._rect_pt.topLeft()))
        self.props.setdefault("p2", QPointF(self._rect_pt.bottomRight()))
        self._sync_rect_from_points()

    def _sync_rect_from_points(self) -> None:
        p1, p2 = self.props["p1"], self.props["p2"]
        self._rect_pt = QRectF(p1, p2).normalized()
        self._apply()

    def _local(self, pt_points: QPointF) -> QPointF:
        z = self._zoom
        return QPointF(pt_points.x() * z - self.pos().x(),
                       pt_points.y() * z - self.pos().y())

    def _handle_points(self) -> Dict[str, QPointF]:
        return {"p1": self._local(self.props["p1"]), "p2": self._local(self.props["p2"])}

    def paint_content(self, painter: QPainter) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        stroke = self.props.get("stroke") or QColor(0, 0, 0)
        width = self.props.get("width", 1.0) * self._zoom
        painter.setPen(QPen(stroke, width))
        painter.drawLine(self._local(self.props["p1"]), self._local(self.props["p2"]))

    def paint(self, painter: QPainter, option, widget=None) -> None:
        self.paint_content(painter)
        if self.isSelected():
            painter.setPen(QPen(QColor(40, 110, 220), 1))
            painter.setBrush(QBrush(QColor(255, 255, 255)))
            for pt in self._handle_points().values():
                painter.drawRect(QRectF(pt.x() - HANDLE, pt.y() - HANDLE,
                                        2 * HANDLE, 2 * HANDLE))

    def mouseMoveEvent(self, event) -> None:
        if self._resize_handle in ("p1", "p2"):
            z = self._zoom or 1.0
            parent_pos = self.mapToParent(event.pos())
            self.props[self._resize_handle] = QPointF(parent_pos.x() / z, parent_pos.y() / z)
            self._sync_rect_from_points()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        # translation drag: move both endpoints by the delta, then normalise
        if self._resize_handle is None:
            z = self._zoom or 1.0
            old_tl = self._rect_pt.topLeft()
            new_tl = QPointF(self.pos().x() / z, self.pos().y() / z)
            d = new_tl - old_tl
            self.props["p1"] = self.props["p1"] + d
            self.props["p2"] = self.props["p2"] + d
            self._sync_rect_from_points()
        super().mouseReleaseEvent(event)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)

    def bake(self, page: "fitz.Page") -> None:
        p1, p2 = self.props["p1"], self.props["p2"]
        page.draw_line(fitz.Point(p1.x(), p1.y()), fitz.Point(p2.x(), p2.y()),
                       color=qcolor_to_fitz(self.props.get("stroke")) or (0, 0, 0),
                       width=self.props.get("width", 1.0))


class TextItem(EditableItem):
    obj_type = "text"

    _FLAGS = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
                 | Qt.TextFlag.TextWordWrap)

    def _make_font(self) -> QFont:
        font = QFont(self.props.get("fontfamily", "Sans Serif"))
        font.setBold(bool(self.props.get("bold", False)))
        font.setItalic(bool(self.props.get("italic", False)))
        return font

    def _fitted_pixel_size(self, rect: QRectF) -> float:
        """Largest pixel size (<= requested) whose wrapped text fits the box.

        Mirrors the shrink-to-fit used when baking so on-screen matches saved.
        """
        from PySide6.QtGui import QFontMetricsF
        text = self.props.get("text", "")
        requested = max(1.0, self.props.get("fontsize", 12.0) * self._zoom)
        if not text.strip() or rect.width() <= 2 or rect.height() <= 2:
            return requested
        font = self._make_font()
        size = requested
        while size > 3:
            font.setPixelSize(max(1, int(round(size))))
            fm = QFontMetricsF(font)
            bounds = fm.boundingRect(QRectF(0, 0, rect.width(), 1e6), self._FLAGS, text)
            if bounds.height() <= rect.height() and bounds.width() <= rect.width() + 0.5:
                break
            size *= 0.92
        return max(3.0, size)

    def paint_content(self, painter: QPainter) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        color = self.props.get("color") or QColor(0, 0, 0)
        rect = self.content_rect().adjusted(2, 1, -2, -1)
        font = self._make_font()
        font.setPixelSize(max(1, int(round(self._fitted_pixel_size(rect)))))
        painter.setFont(font)
        painter.setPen(QPen(color))
        painter.drawText(rect, self._FLAGS, self.props.get("text", ""))

    def bake(self, page: "fitz.Page") -> None:
        from ...textfit import insert_textbox_fit
        from ...fonts import base14_alias
        r = self._rect_pt
        alias = base14_alias(self.props.get("bold", False), self.props.get("italic", False),
                             self.props.get("serif", False), self.props.get("mono", False))
        insert_textbox_fit(
            page, fitz.Rect(r.left(), r.top(), r.right(), r.bottom()),
            self.props.get("text", ""),
            self.props.get("fontsize", 12.0),
            qcolor_to_fitz(self.props.get("color")) or (0, 0, 0),
            fontname=alias)


class ImageItem(EditableItem):
    obj_type = "image"

    def __init__(self, page_index, rect_pt, props=None) -> None:
        super().__init__(page_index, rect_pt, props)
        self._pixmap: Optional[QPixmap] = None
        data = self.props.get("image_bytes")
        if data:
            img = QImage.fromData(data)
            if not img.isNull():
                self._pixmap = QPixmap.fromImage(img)

    def paint_content(self, painter: QPainter) -> None:
        if self._pixmap is not None:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawPixmap(self.content_rect(), self._pixmap,
                               QRectF(self._pixmap.rect()))
        else:
            painter.fillRect(self.content_rect(), QColor(220, 220, 220))

    def bake(self, page: "fitz.Page") -> None:
        r = self._rect_pt
        data = self.props.get("image_bytes")
        if data:
            page.insert_image(fitz.Rect(r.left(), r.top(), r.right(), r.bottom()),
                              stream=data)


def _fitz_rgb(t) -> Optional[QColor]:
    if not t:
        return None
    return QColor.fromRgbF(float(t[0]), float(t[1]), float(t[2]))


class DrawingItem(EditableItem):
    """An existing vector drawing lifted into a movable/resizable object.

    Stores the original paths (from ``page.get_drawings()``) plus their bounding
    box; both display and bake replay the paths scaled from that box into the
    item's current rectangle. Node-level path editing is out of scope.
    """

    obj_type = "drawing"

    def _paths(self):
        return self.props.get("paths", [])

    def _orig(self):
        return self.props.get("orig_rect")

    @staticmethod
    def _build_path(path, f) -> QPainterPath:
        qp = QPainterPath()
        started = False
        for item in path.get("items", []):
            op = item[0]
            if op == "l":
                if not started:
                    qp.moveTo(f(item[1])); started = True
                qp.lineTo(f(item[2]))
            elif op == "c":
                if not started:
                    qp.moveTo(f(item[1])); started = True
                qp.cubicTo(f(item[2]), f(item[3]), f(item[4]))
            elif op == "re":
                r = item[1]
                qp.addRect(QRectF(f(r.tl), f(r.br)))
            elif op == "qu":
                q = item[1]
                qp.addPolygon(QPolygonF([f(q.ul), f(q.ur), f(q.lr), f(q.ll)]))
                qp.closeSubpath()
        if path.get("closePath"):
            qp.closeSubpath()
        return qp

    def paint_content(self, painter: QPainter) -> None:
        orig = self._orig()
        paths = self._paths()
        if not paths or orig is None or orig.width <= 0 or orig.height <= 0:
            painter.fillRect(self.content_rect(), QColor(230, 230, 230))
            return
        sx = self._w / orig.width
        sy = self._h / orig.height
        avg = (sx + sy) / 2

        def f(pt):
            return QPointF((pt.x - orig.x0) * sx, (pt.y - orig.y0) * sy)

        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for path in paths:
            qp = self._build_path(path, f)
            stroke = _fitz_rgb(path.get("color"))
            fill = _fitz_rgb(path.get("fill"))
            width = (path.get("width") or 0) * avg
            painter.setPen(QPen(stroke, width) if stroke else QPen(Qt.PenStyle.NoPen))
            painter.setBrush(QBrush(fill) if fill else QBrush(Qt.BrushStyle.NoBrush))
            painter.drawPath(qp)

    def bake(self, page: "fitz.Page") -> None:
        orig = self._orig()
        paths = self._paths()
        if not paths or orig is None or orig.width <= 0 or orig.height <= 0:
            return
        cur = self._rect_pt
        sx = cur.width() / orig.width
        sy = cur.height() / orig.height
        avg = (sx + sy) / 2

        def T(pt):
            return fitz.Point(cur.left() + (pt.x - orig.x0) * sx,
                              cur.top() + (pt.y - orig.y0) * sy)

        shape = page.new_shape()
        for path in paths:
            for item in path.get("items", []):
                op = item[0]
                if op == "l":
                    shape.draw_line(T(item[1]), T(item[2]))
                elif op == "c":
                    shape.draw_bezier(T(item[1]), T(item[2]), T(item[3]), T(item[4]))
                elif op == "re":
                    r = item[1]
                    shape.draw_rect(fitz.Rect(T(r.tl), T(r.br)))
                elif op == "qu":
                    q = item[1]
                    shape.draw_quad(fitz.Quad(T(q.ul), T(q.ur), T(q.ll), T(q.lr)))
            shape.finish(color=path.get("color"), fill=path.get("fill"),
                         width=(path.get("width") or 0) * avg or 0,
                         closePath=bool(path.get("closePath")),
                         even_odd=bool(path.get("even_odd", False)))
        shape.commit()


_REGISTRY = {
    "rect": RectItem, "ellipse": EllipseItem, "line": LineItem,
    "text": TextItem, "image": ImageItem, "drawing": DrawingItem,
}


def make_item(obj_type: str, page_index: int, rect_pt: QRectF,
              props: Optional[Dict] = None) -> EditableItem:
    return _REGISTRY[obj_type](page_index, rect_pt, props)
