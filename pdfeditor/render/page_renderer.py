"""Asynchronous page rendering with an LRU pixmap cache.

Render requests run on a QThreadPool, but every PyMuPDF call is guarded by the
document's mutex so the fitz.Document is only ever touched by one thread at a
time. Results are delivered on the GUI thread via the ``pixmapReady`` signal.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional, Set, Tuple

import fitz
from PySide6.QtCore import (QMutexLocker, QObject, QRunnable, QThreadPool,
                            Signal)
from PySide6.QtGui import QImage, QPixmap

from ..document import PdfDocument


def render_key(page_index: int, zoom: float, dpr: float) -> Tuple[int, int, int]:
    """Cache key. Zoom/dpr are quantised so tiny float drift doesn't miss."""
    return (page_index, round(zoom * 1000), round(dpr * 100))


@dataclass
class RenderResult:
    page_index: int
    zoom: float
    dpr: float
    pixmap: QPixmap
    key: Tuple[int, int, int]


class _TaskSignals(QObject):
    finished = Signal(object)  # RenderResult


class _RenderTask(QRunnable):
    def __init__(self, document: PdfDocument, page_index: int, zoom: float,
                 dpr: float, key, signals: _TaskSignals) -> None:
        super().__init__()
        self._document = document
        self._page_index = page_index
        self._zoom = zoom
        self._dpr = dpr
        self._key = key
        self._signals = signals

    def run(self) -> None:
        doc = self._document.doc
        if doc is None:
            return
        scale = self._zoom * self._dpr
        try:
            with QMutexLocker(self._document.lock):
                if self._document.doc is not doc or self._page_index >= doc.page_count:
                    return
                page = doc.load_page(self._page_index)
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                # Copy the buffer while still under the lock and before pix is freed.
                image = QImage(pix.samples, pix.width, pix.height, pix.stride,
                               QImage.Format.Format_RGB888).copy()
        except Exception:
            return
        image.setDevicePixelRatio(self._dpr)
        pixmap = QPixmap.fromImage(image)
        self._signals.finished.emit(
            RenderResult(self._page_index, self._zoom, self._dpr, pixmap, self._key))


class PageRenderer(QObject):
    """Renders pages off the GUI thread and caches the resulting pixmaps."""

    pixmapReady = Signal(object)  # RenderResult

    def __init__(self, document: PdfDocument, cache_size: int = 48,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._document = document
        self._pool = QThreadPool.globalInstance()
        self._cache: "OrderedDict[tuple, QPixmap]" = OrderedDict()
        self._cache_size = cache_size
        self._inflight: Set[tuple] = set()
        self._signals = _TaskSignals()
        self._signals.finished.connect(self._on_finished)

    def clear_cache(self) -> None:
        self._cache.clear()
        self._inflight.clear()

    def invalidate_page(self, page_index: int) -> None:
        for key in [k for k in self._cache if k[0] == page_index]:
            del self._cache[key]

    def cached(self, page_index: int, zoom: float, dpr: float) -> Optional[QPixmap]:
        key = render_key(page_index, zoom, dpr)
        pix = self._cache.get(key)
        if pix is not None:
            self._cache.move_to_end(key)
        return pix

    def request(self, page_index: int, zoom: float, dpr: float) -> Optional[QPixmap]:
        """Return a cached pixmap immediately, or schedule a render and return None."""
        key = render_key(page_index, zoom, dpr)
        pix = self._cache.get(key)
        if pix is not None:
            self._cache.move_to_end(key)
            return pix
        if key in self._inflight or not self._document.is_open:
            return None
        self._inflight.add(key)
        task = _RenderTask(self._document, page_index, zoom, dpr, key, self._signals)
        self._pool.start(task)
        return None

    def _on_finished(self, result: RenderResult) -> None:
        self._inflight.discard(result.key)
        self._cache[result.key] = result.pixmap
        self._cache.move_to_end(result.key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        self.pixmapReady.emit(result)
