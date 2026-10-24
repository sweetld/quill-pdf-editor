"""PageItem — one page in the document view's scene.

Its scene geometry is the page size in points multiplied by the current zoom.
It draws the rendered pixmap (or a placeholder), plus search-highlight and
selection overlays in page-point coordinates mapped through the zoom.
"""
from __future__ import annotations

from typing import List, Optional

import fitz
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QGraphicsItem, QStyleOptionGraphicsItem, QWidget)

PAGE_SHADOW = 6.0  # px of drop shadow drawn to the right/bottom


class PageItem(QGraphicsItem):
    def __init__(self, page_index: int, width_pt: float, height_pt: float,
                 zoom: float = 1.0) -> None:
        super().__init__()
        self.page_index = page_index
        self.width_pt = width_pt
        self.height_pt = height_pt
        self._zoom = zoom
        self._pixmap: Optional[QPixmap] = None
        self._search_rects: List[fitz.Rect] = []
        self._selection_rects: List[fitz.Rect] = []
        self._active_search: Optional[fitz.Rect] = None
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemUsesExtendedStyleOption, True)

    # ---- geometry --------------------------------------------------------
    @property
    def zoom(self) -> float:
        return self._zoom

    def set_zoom(self, zoom: float) -> None:
        if zoom != self._zoom:
            self.prepareGeometryChange()
            self._zoom = zoom
            # cached pixmap is for the old zoom; drop it to avoid a blurry frame
            self._pixmap = None
            self.update()

    def display_size(self):
        return self.width_pt * self._zoom, self.height_pt * self._zoom

    def boundingRect(self) -> QRectF:
        w = self.width_pt * self._zoom
        h = self.height_pt * self._zoom
        return QRectF(0, 0, w + PAGE_SHADOW, h + PAGE_SHADOW)

    def page_rect(self) -> QRectF:
        w = self.width_pt * self._zoom
        h = self.height_pt * self._zoom
        return QRectF(0, 0, w, h)

    # ---- content ---------------------------------------------------------
    def set_pixmap(self, pixmap: QPixmap) -> None:
        self._pixmap = pixmap
        self.update()

    def has_pixmap(self) -> bool:
        return self._pixmap is not None

    def clear_pixmap(self) -> None:
        self._pixmap = None
        self.update()

    def set_search_rects(self, rects: List[fitz.Rect],
                         active: Optional[fitz.Rect] = None) -> None:
        self._search_rects = rects
        self._active_search = active
        self.update()

    def set_selection_rects(self, rects: List[fitz.Rect]) -> None:
        self._selection_rects = rects
        self.update()

    # ---- coordinate mapping ---------------------------------------------
    def to_pdf_point(self, local_x: float, local_y: float) -> fitz.Point:
        return fitz.Point(local_x / self._zoom, local_y / self._zoom)

    def pdf_rect_to_local(self, rect: fitz.Rect) -> QRectF:
        z = self._zoom
        return QRectF(rect.x0 * z, rect.y0 * z,
                      (rect.x1 - rect.x0) * z, (rect.y1 - rect.y0) * z)

    # ---- painting --------------------------------------------------------
    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem,
              widget: Optional[QWidget] = None) -> None:
        page = self.page_rect()

        # drop shadow
        painter.fillRect(QRectF(PAGE_SHADOW, page.height(), page.width(), PAGE_SHADOW),
                         QColor(0, 0, 0, 40))
        painter.fillRect(QRectF(page.width(), PAGE_SHADOW, PAGE_SHADOW, page.height()),
                         QColor(0, 0, 0, 40))

        if self._pixmap is not None:
            painter.drawPixmap(page, self._pixmap, QRectF(self._pixmap.rect()))
        else:
            painter.fillRect(page, QColor(255, 255, 255))
            painter.setPen(QPen(QColor(180, 180, 180)))
            painter.drawText(page, Qt.AlignmentFlag.AlignCenter, "…")

        painter.setPen(QPen(QColor(160, 160, 160)))
        painter.drawRect(page)

        # selection overlay (text marquee)
        if self._selection_rects:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(60, 120, 220, 70))
            for r in self._selection_rects:
                painter.drawRect(self.pdf_rect_to_local(r))

        # search highlights
        if self._search_rects:
            painter.setPen(Qt.PenStyle.NoPen)
            for r in self._search_rects:
                is_active = self._active_search is not None and r == self._active_search
                painter.setBrush(QColor(255, 160, 0, 150) if is_active
                                 else QColor(255, 235, 0, 120))
                painter.drawRect(self.pdf_rect_to_local(r))
