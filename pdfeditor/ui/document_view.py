"""DocumentView — a scrollable, continuous multi-page canvas built on QGraphicsView.

Responsibilities:
  * lay pages out vertically (continuous scroll), centred horizontally
  * zoom (explicit %, fit-width, fit-page) with the viewport centre anchored
  * request/receive async page renders and place them on the right PageItem
  * track the "current" page from the viewport and expose navigation
  * forward mouse events to the active Tool
"""
from __future__ import annotations

from enum import Enum, auto
from typing import Dict, List, Optional

import fitz
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView

from ..document import PdfDocument
from ..render import PageRenderer, RenderResult
from .page_item import PAGE_SHADOW, PageItem
from .tools.base_tool import Tool, ToolContext

PAGE_GAP = 14.0      # vertical gap between pages (scene px)
PAGE_MARGIN = 20.0   # margin around the whole stack
MIN_ZOOM = 0.1
MAX_ZOOM = 8.0


class ZoomMode(Enum):
    CUSTOM = auto()
    FIT_WIDTH = auto()
    FIT_PAGE = auto()


class DocumentView(QGraphicsView):
    currentPageChanged = Signal(int)   # 0-based
    zoomChanged = Signal(float)        # 1.0 == 100%
    selectionChanged = Signal()
    deleteKeyPressed = Signal()        # Delete/Backspace with objects selected

    def __init__(self, document: PdfDocument, renderer: PageRenderer, parent=None) -> None:
        super().__init__(parent)
        self._document = document
        self._renderer = renderer
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setBackgroundBrush(QColor(82, 84, 88))
        self.setRenderHints(QPainter.RenderHint.Antialiasing
                            | QPainter.RenderHint.SmoothPixmapTransform)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.setMouseTracking(True)

        self._items: List[PageItem] = []
        self._zoom = 1.0
        self._zoom_mode = ZoomMode.FIT_WIDTH
        self._current_page = 0

        self._tool: Optional[Tool] = None
        self._tool_ctx = ToolContext(view=self)
        self._object_manager = None
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._renderer.pixmapReady.connect(self._on_pixmap_ready)
        self.verticalScrollBar().valueChanged.connect(self._on_scrolled)

    # ---- tool API --------------------------------------------------------
    def set_tool(self, tool: Tool) -> None:
        if self._tool is not None:
            self._tool.deactivate()
        self._tool = tool
        if tool is not None:
            tool.activate()
            self.viewport().setCursor(tool.cursor)

    def current_tool(self) -> Optional[Tool]:
        return self._tool

    def document(self) -> PdfDocument:
        return self._document

    def set_object_manager(self, manager) -> None:
        self._object_manager = manager

    def object_manager(self):
        return self._object_manager

    def page_items(self) -> List[PageItem]:
        return self._items

    # ---- build / layout --------------------------------------------------
    def reload(self, preserve: bool = False) -> None:
        prev_page = self._current_page if preserve else 0
        self._scene.clear()
        self._items.clear()
        self._current_page = 0
        if not self._document.is_open:
            self._scene.setSceneRect(QRectF())
            return
        sizes = self._document.page_sizes_pt()
        for i, (w, h) in enumerate(sizes):
            item = PageItem(i, w, h, self._zoom)
            self._scene.addItem(item)
            self._items.append(item)
        if preserve:
            self._relayout()
            self.go_to_page(min(prev_page, len(self._items) - 1), center=False)
        else:
            self.apply_zoom_mode(initial=True)

    def _relayout(self) -> None:
        if not self._items:
            return
        max_w = max(it.width_pt * self._zoom for it in self._items)
        content_w = max_w + PAGE_SHADOW
        y = PAGE_MARGIN
        for it in self._items:
            it.set_zoom(self._zoom)
            dw, dh = it.display_size()
            x = PAGE_MARGIN + (content_w - (dw + PAGE_SHADOW)) / 2
            it.setPos(x, y)
            y += dh + PAGE_GAP
        total_w = content_w + 2 * PAGE_MARGIN
        total_h = y - PAGE_GAP + PAGE_MARGIN
        self._scene.setSceneRect(0, 0, total_w, total_h)
        self._update_visible()

    # ---- zoom ------------------------------------------------------------
    def zoom(self) -> float:
        return self._zoom

    def zoom_mode(self) -> ZoomMode:
        return self._zoom_mode

    def set_zoom(self, zoom: float, mode: ZoomMode = ZoomMode.CUSTOM,
                anchor_center: bool = True) -> None:
        zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))
        if abs(zoom - self._zoom) < 1e-4 and mode == self._zoom_mode:
            return
        # anchor: remember the scene point under the viewport centre
        anchor_scene = None
        if anchor_center and self._items:
            anchor_scene = self.mapToScene(self.viewport().rect().center())
            page_before = self._current_page
        self._zoom = zoom
        self._zoom_mode = mode
        self._renderer.clear_cache()
        self._relayout()
        if anchor_scene is not None:
            # keep the same page roughly centred after the re-layout
            self.go_to_page(page_before, center=True)
        self.zoomChanged.emit(self._zoom)

    def zoom_in(self) -> None:
        self.set_zoom(self._zoom * 1.25)

    def zoom_out(self) -> None:
        self.set_zoom(self._zoom / 1.25)

    def apply_zoom_mode(self, initial: bool = False) -> None:
        if not self._items:
            return
        if self._zoom_mode == ZoomMode.FIT_WIDTH:
            self.fit_width()
        elif self._zoom_mode == ZoomMode.FIT_PAGE:
            self.fit_page()
        elif initial:
            self._relayout()

    def fit_width(self) -> None:
        if not self._items:
            return
        avail = self.viewport().width() - 2 * PAGE_MARGIN - PAGE_SHADOW - 2
        page_w = max(it.width_pt for it in self._items)
        z = max(MIN_ZOOM, min(MAX_ZOOM, avail / page_w)) if page_w else 1.0
        page = self._current_page
        self._zoom = z
        self._zoom_mode = ZoomMode.FIT_WIDTH
        self._renderer.clear_cache()
        self._relayout()
        self.go_to_page(page, center=False)
        self.zoomChanged.emit(self._zoom)

    def fit_page(self) -> None:
        if not self._items:
            return
        it = self._items[self._current_page]
        avail_w = self.viewport().width() - 2 * PAGE_MARGIN - PAGE_SHADOW - 2
        avail_h = self.viewport().height() - 2 * PAGE_MARGIN - PAGE_SHADOW - 2
        z = min(avail_w / it.width_pt, avail_h / it.height_pt)
        z = max(MIN_ZOOM, min(MAX_ZOOM, z))
        page = self._current_page
        self._zoom = z
        self._zoom_mode = ZoomMode.FIT_PAGE
        self._renderer.clear_cache()
        self._relayout()
        self.go_to_page(page, center=True)
        self.zoomChanged.emit(self._zoom)

    # ---- navigation ------------------------------------------------------
    def current_page(self) -> int:
        return self._current_page

    def go_to_page(self, index: int, center: bool = True) -> None:
        if not self._items:
            return
        index = max(0, min(len(self._items) - 1, index))
        it = self._items[index]
        if center:
            self.centerOn(it.mapToScene(it.page_rect().center()))
        else:
            top = it.mapToScene(it.page_rect().topLeft())
            self.centerOn(self.mapToScene(self.viewport().rect().center()).x(),
                          top.y() + self.viewport().height() / 2 - PAGE_MARGIN)
        self._set_current_page(index)
        self._update_visible()

    def _set_current_page(self, index: int) -> None:
        if index != self._current_page:
            self._current_page = index
            self.currentPageChanged.emit(index)

    def _compute_current_page(self) -> int:
        center = self.mapToScene(self.viewport().rect().center())
        best_i, best_d = 0, None
        for i, it in enumerate(self._items):
            r = it.mapToScene(it.page_rect()).boundingRect()
            cy = r.center().y()
            d = abs(cy - center.y())
            if best_d is None or d < best_d:
                best_i, best_d = i, d
        return best_i

    # ---- rendering -------------------------------------------------------
    def _dpr(self) -> float:
        scr = self.screen()
        return float(scr.devicePixelRatio()) if scr else 1.0

    def _update_visible(self) -> None:
        if not self._items:
            return
        vis = self.mapToScene(self.viewport().rect()).boundingRect()
        vis.adjust(0, -vis.height() * 0.5, 0, vis.height() * 0.5)  # prefetch buffer
        dpr = self._dpr()
        for it in self._items:
            r = it.mapToScene(it.boundingRect()).boundingRect()
            if r.intersects(vis):
                pix = self._renderer.request(it.page_index, self._zoom, dpr)
                if pix is not None and not it.has_pixmap():
                    it.set_pixmap(pix)

    def _on_pixmap_ready(self, result: RenderResult) -> None:
        if abs(result.zoom - self._zoom) > 1e-4:
            return  # stale (zoom changed since request)
        if 0 <= result.page_index < len(self._items):
            self._items[result.page_index].set_pixmap(result.pixmap)

    def _on_scrolled(self, _value: int) -> None:
        self._update_visible()
        self._set_current_page(self._compute_current_page())

    # ---- search / selection overlays ------------------------------------
    def set_search_results(self, per_page: Dict[int, List[fitz.Rect]],
                           active_page: int = -1,
                           active_rect: Optional[fitz.Rect] = None) -> None:
        for it in self._items:
            rects = per_page.get(it.page_index, [])
            act = active_rect if it.page_index == active_page else None
            it.set_search_rects(rects, act)

    def clear_selection(self) -> None:
        for it in self._items:
            it.set_selection_rects([])

    # ---- geometry helpers for tools -------------------------------------
    def page_item_at(self, scene_pos: QPointF) -> Optional[PageItem]:
        for it in self._items:
            local = it.mapFromScene(scene_pos)
            if it.page_rect().contains(local):
                return it
        return None

    def page_item_for(self, page_index: int) -> Optional[PageItem]:
        if 0 <= page_index < len(self._items):
            return self._items[page_index]
        return None

    def refresh_page(self, page_index: int) -> None:
        """Drop a page's cached image and re-request it (after a content edit)."""
        if 0 <= page_index < len(self._items):
            self._items[page_index].clear_pixmap()
            self._update_visible()

    # ---- Qt events -------------------------------------------------------
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._zoom_mode in (ZoomMode.FIT_WIDTH, ZoomMode.FIT_PAGE):
            self.apply_zoom_mode()
        else:
            self._update_visible()

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta > 0:
                self.zoom_in()
            elif delta < 0:
                self.zoom_out()
            event.accept()
        else:
            super().wheelEvent(event)

    def _scene_pos(self, event: QMouseEvent) -> QPointF:
        return self.mapToScene(event.position().toPoint())

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._tool and self._tool.mouse_press(event, self._scene_pos(event)):
            self.selectionChanged.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._tool and self._tool.mouse_move(event, self._scene_pos(event)):
            self.selectionChanged.emit()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._tool and self._tool.mouse_release(event, self._scene_pos(event)):
            self.selectionChanged.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if self._scene.selectedItems():
                self.deleteKeyPressed.emit()
                event.accept()
                return
        super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if self._tool and self._tool.mouse_double_click(event, self._scene_pos(event)):
            self.selectionChanged.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)
