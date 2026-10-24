"""MainWindow — assembles the Master PDF Editor–style chrome around DocumentView.

Layout mirrors Master PDF Editor's arrangement: top menu bar, top main toolbar,
a left vertical tool toolbar, a tabbed left dock (Pages / Bookmarks), the central
document view, and a status bar with page + zoom controls.
"""
from __future__ import annotations

import json
import os
from typing import List, Optional

import fitz
from PySide6.QtCore import (QFileSystemWatcher, QMutexLocker, QSettings, QSize, Qt,
                            QTimer)
from PySide6.QtGui import (QAction, QActionGroup, QGuiApplication, QIcon,
                           QKeySequence, QPainter, QUndoStack)
from PySide6.QtWidgets import (QComboBox, QDockWidget, QFileDialog, QLabel,
                               QMainWindow, QMessageBox, QSpinBox, QTabWidget,
                               QToolBar, QVBoxLayout, QWidget)

from .. import APP_NAME, ORG_NAME, __version__
from ..document import PdfDocument
from ..document.undo import (DeletePagesCommand, DuplicatePageCommand,
                             EditTextCommand, InsertBlankCommand,
                             InsertFromFileCommand, MovePageCommand,
                             ReorderCommand, RotateCommand)
from ..render import PageRenderer
from .document_view import DocumentView, ZoomMode
from .objects import ObjectManager
from .objects.object_commands import RemoveObjectsCommand
from .panels import OutlinePanel, PropertiesPanel, ThumbnailPanel
from .search_bar import SearchBar
from .tools import PanTool, SelectTextTool
from .tools.create_tools import (InsertImageTool, InsertTextTool, ShapeTool)
from .tools.edit_text_tool import EditTextTool
from .tools.select_object_tool import SelectObjectTool

MAX_RECENT = 8
ZOOM_PRESETS = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0]


def themed(name: str, fallback_text: str = "") -> QIcon:
    icon = QIcon.fromTheme(name)
    return icon if not icon.isNull() else QIcon()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1200, 820)

        self._settings = QSettings(ORG_NAME, APP_NAME)
        self._document = PdfDocument(self)
        self._renderer = PageRenderer(self._document, parent=self)
        self._undo_stack = QUndoStack(self)
        self._undo_stack.setUndoLimit(50)

        self._view = DocumentView(self._document, self._renderer, self)
        self._objects = ObjectManager(self._view, self)
        self._view.set_object_manager(self._objects)
        self._search = SearchBar(self._document, self)
        self._search.hide()

        # Follow external changes to the open file (e.g. an AI agent editing a
        # live-preview copy): reload automatically when there are no local edits.
        self._watcher = QFileSystemWatcher(self)
        self._disk_sig = None
        self._disk_timer = QTimer(self)
        self._disk_timer.setSingleShot(True)
        self._disk_timer.setInterval(200)
        self._disk_timer.timeout.connect(self._check_disk_change)

        central = QWidget(self)
        vbox = QVBoxLayout(central)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)
        vbox.addWidget(self._search)
        vbox.addWidget(self._view, 1)
        self.setCentralWidget(central)

        self._recent: List[str] = self._settings.value("recentFiles", [], type=list) or []

        self._build_tools()
        self._create_actions()
        self._create_menus()
        self._create_toolbars()
        self._create_docks()
        self._create_statusbar()
        self._connect_signals()

        self._update_ui_state()
        self._restore_geometry()

    # ---- tools -----------------------------------------------------------
    def _build_tools(self) -> None:
        ctx = self._view._tool_ctx
        push = self._undo_stack.push
        self._pan_tool = PanTool(ctx)
        self._select_tool = SelectTextTool(ctx)
        self._select_object_tool = SelectObjectTool(
            ctx, edit_text=self._edit_text_object, lift=self._lift_object)
        self._insert_text_tool = InsertTextTool(ctx, self._objects, push,
                                                edit_text=self._edit_text_object)
        self._insert_image_tool = InsertImageTool(ctx, self._objects, push, self._pick_image)
        self._rect_tool = ShapeTool(ctx, self._objects, push, "rect")
        self._ellipse_tool = ShapeTool(ctx, self._objects, push, "ellipse")
        self._line_tool = ShapeTool(ctx, self._objects, push, "line")
        self._edit_text_tool = EditTextTool(ctx, push, self._make_edit_text_command)
        self._view.set_tool(self._select_tool)

    def _make_edit_text_command(self, index, rect, text, size, color, fontname="helv"):
        return EditTextCommand(self._document, index, rect, text, size, color, fontname)

    def _lift_object(self, index: int, pt) -> bool:
        """Convert existing page content under ``pt`` (text, image, or vector
        drawing) into an editable object. Returns True if something was lifted."""
        return (self._lift_text(index, pt)
                or self._lift_image(index, pt)
                or self._lift_graphics(index, pt))

    def _lift_text(self, index: int, pt) -> bool:
        info = self._document.text_line_at(index, pt)
        if not info:
            return False
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor
        from .objects.editable_item import make_item
        from .objects.object_commands import LiftObjectCommand
        from ..fonts import qt_family, style_from_flags
        rect, text, size, color_int, flags, fontname = info
        try:
            r, g, b = fitz.sRGB_to_rgb(color_int)
        except Exception:
            r, g, b = (color_int >> 16) & 255, (color_int >> 8) & 255, color_int & 255
        style = style_from_flags(flags, fontname)
        item = make_item("text", index,
                         QRectF(rect.x0, rect.y0, rect.width, rect.height),
                         {"text": text, "fontsize": size, "color": QColor(r, g, b),
                          "fontfamily": qt_family(style["serif"], style["mono"]),
                          "bold": style["bold"], "italic": style["italic"],
                          "serif": style["serif"], "mono": style["mono"]})
        self._undo_stack.push(LiftObjectCommand(self._document, self._objects,
                                                index, rect, item, "Edit existing text"))
        return True

    def _lift_image(self, index: int, pt) -> bool:
        info = self._document.image_at(index, pt)
        if not info:
            return False
        from PySide6.QtCore import QRectF
        from .objects.editable_item import make_item
        from .objects.object_commands import LiftObjectCommand
        rect, data = info
        item = make_item("image", index,
                         QRectF(rect.x0, rect.y0, rect.width, rect.height),
                         {"image_bytes": data})
        self._undo_stack.push(LiftObjectCommand(
            self._document, self._objects, index, rect, item,
            "Edit existing image", redact_images=True))
        return True

    def _lift_graphics(self, index: int, pt) -> bool:
        info = self._document.graphics_at(index, pt)
        if not info:
            return False
        from PySide6.QtCore import QRectF
        from .objects.editable_item import make_item
        from .objects.object_commands import LiftObjectCommand
        rect, paths = info
        item = make_item("drawing", index,
                         QRectF(rect.x0, rect.y0, rect.width, rect.height),
                         {"paths": paths, "orig_rect": rect})
        self._undo_stack.push(LiftObjectCommand(
            self._document, self._objects, index, rect, item,
            "Edit existing graphic", redact_graphics=True))
        return True

    def _edit_text_object(self, item) -> None:
        """Edit a text object's content via a dialog (double-click / on create)."""
        from PySide6.QtWidgets import QInputDialog
        from .objects.editable_item import clone_props
        from .objects.object_commands import PropertyCommand
        current = item.props.get("text", "")
        text, ok = QInputDialog.getMultiLineText(self, "Text", "Text:", current)
        if not ok:
            return
        old = clone_props(item.props)
        new = clone_props(item.props)
        new["text"] = text
        item.props = new
        item.update()
        self._undo_stack.push(PropertyCommand(item, old, new, "Edit text"))

    def _pick_image(self) -> Optional[bytes]:
        path, _ = QFileDialog.getOpenFileName(
            self, "Insert Image", os.path.expanduser("~"),
            "Images (*.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff)")
        if not path:
            return None
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except OSError as exc:
            QMessageBox.critical(self, APP_NAME, f"Could not read image:\n{exc}")
            return None

    # ---- actions ---------------------------------------------------------
    def _create_actions(self) -> None:
        self.act_open = QAction(themed("document-open"), "&Open…", self)
        self.act_open.setShortcut(QKeySequence.StandardKey.Open)
        self.act_open.triggered.connect(self.open_file_dialog)

        self.act_save = QAction(themed("document-save"), "&Save", self)
        self.act_save.setShortcut(QKeySequence.StandardKey.Save)
        self.act_save.triggered.connect(self.save)

        self.act_save_as = QAction(themed("document-save-as"), "Save &As…", self)
        self.act_save_as.setShortcut(QKeySequence.StandardKey.SaveAs)
        self.act_save_as.triggered.connect(self.save_as)

        self.act_print = QAction(themed("document-print"), "&Print…", self)
        self.act_print.setShortcut(QKeySequence.StandardKey.Print)
        self.act_print.triggered.connect(self.print_document)

        self.act_close = QAction("&Close", self)
        self.act_close.setShortcut(QKeySequence.StandardKey.Close)
        self.act_close.triggered.connect(self.close_document)

        self.act_quit = QAction("&Quit", self)
        self.act_quit.setShortcut(QKeySequence.StandardKey.Quit)
        self.act_quit.triggered.connect(self.close)

        self.act_undo = self._undo_stack.createUndoAction(self, "&Undo")
        self.act_undo.setShortcut(QKeySequence.StandardKey.Undo)
        self.act_undo.setIcon(themed("edit-undo"))
        self.act_redo = self._undo_stack.createRedoAction(self, "&Redo")
        self.act_redo.setShortcut(QKeySequence.StandardKey.Redo)
        self.act_redo.setIcon(themed("edit-redo"))

        self.act_copy = QAction(themed("edit-copy"), "&Copy", self)
        self.act_copy.setShortcut(QKeySequence.StandardKey.Copy)
        self.act_copy.triggered.connect(self.copy_selection)

        self.act_find = QAction(themed("edit-find"), "&Find…", self)
        self.act_find.setShortcut(QKeySequence.StandardKey.Find)
        self.act_find.triggered.connect(self.show_find)

        self.act_zoom_in = QAction(themed("zoom-in"), "Zoom &In", self)
        self.act_zoom_in.setShortcut(QKeySequence.StandardKey.ZoomIn)
        self.act_zoom_in.triggered.connect(self._view.zoom_in)

        self.act_zoom_out = QAction(themed("zoom-out"), "Zoom &Out", self)
        self.act_zoom_out.setShortcut(QKeySequence.StandardKey.ZoomOut)
        self.act_zoom_out.triggered.connect(self._view.zoom_out)

        self.act_fit_width = QAction(themed("zoom-fit-width"), "Fit &Width", self)
        self.act_fit_width.setShortcut("Ctrl+1")
        self.act_fit_width.triggered.connect(self._view.fit_width)

        self.act_fit_page = QAction(themed("zoom-fit-best"), "Fit &Page", self)
        self.act_fit_page.setShortcut("Ctrl+2")
        self.act_fit_page.triggered.connect(self._view.fit_page)

        self.act_actual_size = QAction(themed("zoom-original"), "&Actual Size", self)
        self.act_actual_size.setShortcut("Ctrl+0")
        self.act_actual_size.triggered.connect(lambda: self._view.set_zoom(1.0))

        self.act_prev = QAction(themed("go-previous"), "Previous Page", self)
        self.act_prev.setShortcut(QKeySequence.StandardKey.MoveToPreviousPage)
        self.act_prev.triggered.connect(self._go_prev)

        self.act_next = QAction(themed("go-next"), "Next Page", self)
        self.act_next.setShortcut(QKeySequence.StandardKey.MoveToNextPage)
        self.act_next.triggered.connect(self._go_next)

        self.act_about = QAction("&About", self)
        self.act_about.triggered.connect(self.show_about)

        # tool actions (mutually exclusive)
        self._tool_group = QActionGroup(self)
        self._tool_group.setExclusive(True)
        self.act_tool_pan = QAction(themed("transform-move"), "Hand / Pan", self)
        self.act_tool_pan.setCheckable(True)
        self.act_tool_pan.triggered.connect(lambda: self._view.set_tool(self._pan_tool))
        self.act_tool_select = QAction(themed("edit-select-all"), "Select Text", self)
        self.act_tool_select.setCheckable(True)
        self.act_tool_select.setChecked(True)
        self.act_tool_select.triggered.connect(lambda: self._view.set_tool(self._select_tool))
        self._tool_group.addAction(self.act_tool_pan)
        self._tool_group.addAction(self.act_tool_select)

        def tool_action(icon, text, tool, checked=False):
            act = QAction(themed(icon), text, self)
            act.setCheckable(True)
            act.setChecked(checked)
            act.triggered.connect(lambda: self._view.set_tool(tool))
            self._tool_group.addAction(act)
            return act

        self.act_tool_object = tool_action("transform-move", "Edit Objects",
                                           self._select_object_tool)
        self.act_tool_edit_text = tool_action("format-text-direction-ltr", "Edit Text",
                                              self._edit_text_tool)
        self.act_tool_insert_text = tool_action("insert-text", "Insert Text Box",
                                                self._insert_text_tool)
        self.act_tool_insert_image = tool_action("insert-image", "Insert Image",
                                                 self._insert_image_tool)
        self.act_tool_rect = tool_action("draw-rectangle", "Rectangle", self._rect_tool)
        self.act_tool_ellipse = tool_action("draw-ellipse", "Ellipse", self._ellipse_tool)
        self.act_tool_line = tool_action("draw-line", "Line", self._line_tool)

        # page-management actions (operate on the current / selected pages)
        self.act_insert_before = QAction("Insert Blank Page &Before…", self)
        self.act_insert_before.triggered.connect(lambda: self._insert_blank(before=True))
        self.act_insert_after = QAction("Insert Blank Page &After…", self)
        self.act_insert_after.triggered.connect(lambda: self._insert_blank(before=False))
        self.act_insert_file = QAction("&Insert Pages from File…", self)
        self.act_insert_file.triggered.connect(self._insert_from_file)
        self.act_duplicate = QAction("&Duplicate Page", self)
        self.act_duplicate.triggered.connect(self._duplicate_current)
        self.act_delete_page = QAction(themed("edit-delete"), "De&lete Page(s)", self)
        self.act_delete_page.triggered.connect(self._delete_current)
        self.act_rotate_left = QAction(themed("object-rotate-left"), "Rotate &Left", self)
        self.act_rotate_left.setShortcut("Ctrl+Shift+Left")
        self.act_rotate_left.triggered.connect(lambda: self._rotate_current(-90))
        self.act_rotate_right = QAction(themed("object-rotate-right"), "Rotate &Right", self)
        self.act_rotate_right.setShortcut("Ctrl+Shift+Right")
        self.act_rotate_right.triggered.connect(lambda: self._rotate_current(90))
        self.act_extract = QAction("&Extract Page(s)…", self)
        self.act_extract.triggered.connect(self._extract_pages)
        self.act_split = QAction("&Split into Single Pages…", self)
        self.act_split.triggered.connect(self._split_document)

    # ---- menus -----------------------------------------------------------
    def _create_menus(self) -> None:
        mb = self.menuBar()

        m_file = mb.addMenu("&File")
        m_file.addAction(self.act_open)
        self._recent_menu = m_file.addMenu("Open &Recent")
        self._rebuild_recent_menu()
        m_file.addSeparator()
        m_file.addAction(self.act_save)
        m_file.addAction(self.act_save_as)
        m_file.addSeparator()
        m_file.addAction(self.act_print)
        m_file.addSeparator()
        m_file.addAction(self.act_close)
        m_file.addAction(self.act_quit)

        m_edit = mb.addMenu("&Edit")
        m_edit.addAction(self.act_undo)
        m_edit.addAction(self.act_redo)
        m_edit.addSeparator()
        m_edit.addAction(self.act_copy)
        m_edit.addSeparator()
        m_edit.addAction(self.act_find)

        m_view = mb.addMenu("&View")
        m_view.addAction(self.act_zoom_in)
        m_view.addAction(self.act_zoom_out)
        m_view.addAction(self.act_actual_size)
        m_view.addAction(self.act_fit_width)
        m_view.addAction(self.act_fit_page)
        m_view.addSeparator()
        m_view.addAction(self.act_prev)
        m_view.addAction(self.act_next)
        self._view_menu = m_view  # panels toggle appended after docks exist

        self._m_document = mb.addMenu("&Document")
        self._m_document.addAction(self.act_insert_before)
        self._m_document.addAction(self.act_insert_after)
        self._m_document.addAction(self.act_insert_file)
        self._m_document.addSeparator()
        self._m_document.addAction(self.act_duplicate)
        self._m_document.addAction(self.act_rotate_left)
        self._m_document.addAction(self.act_rotate_right)
        self._m_document.addSeparator()
        self._m_document.addAction(self.act_extract)
        self._m_document.addAction(self.act_split)
        self._m_document.addAction(self.act_delete_page)

        m_tools = mb.addMenu("&Tools")
        m_tools.addAction(self.act_tool_select)
        m_tools.addAction(self.act_tool_pan)
        m_tools.addAction(self.act_tool_object)
        m_tools.addSeparator()
        m_tools.addAction(self.act_tool_edit_text)
        m_tools.addAction(self.act_tool_insert_text)
        m_tools.addAction(self.act_tool_insert_image)
        m_tools.addSeparator()
        m_tools.addAction(self.act_tool_rect)
        m_tools.addAction(self.act_tool_ellipse)
        m_tools.addAction(self.act_tool_line)
        self._m_forms = mb.addMenu("F&orms")
        self._m_forms.addAction(self._todo("Fill / create form fields — Phase 4"))

        m_help = mb.addMenu("&Help")
        m_help.addAction(self.act_about)

    def _todo(self, text: str) -> QAction:
        a = QAction(text, self)
        a.setEnabled(False)
        return a

    # ---- toolbars --------------------------------------------------------
    def _create_toolbars(self) -> None:
        main_tb = QToolBar("Main", self)
        main_tb.setObjectName("MainToolbar")
        main_tb.setIconSize(QSize(22, 22))
        main_tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, main_tb)
        for act in (self.act_open, self.act_save, self.act_print):
            main_tb.addAction(act)
        main_tb.addSeparator()
        main_tb.addAction(self.act_undo)
        main_tb.addAction(self.act_redo)
        main_tb.addSeparator()
        main_tb.addAction(self.act_zoom_out)

        self._zoom_combo = QComboBox(self)
        self._zoom_combo.setEditable(True)
        self._zoom_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._zoom_combo.setFixedWidth(90)
        for z in ZOOM_PRESETS:
            self._zoom_combo.addItem(f"{int(z * 100)}%", z)
        self._zoom_combo.lineEdit().returnPressed.connect(self._zoom_combo_entered)
        self._zoom_combo.activated.connect(self._zoom_combo_activated)
        main_tb.addWidget(self._zoom_combo)

        main_tb.addAction(self.act_zoom_in)
        main_tb.addAction(self.act_fit_width)
        main_tb.addAction(self.act_fit_page)
        main_tb.addSeparator()
        main_tb.addAction(self.act_prev)

        self._page_spin = QSpinBox(self)
        self._page_spin.setMinimum(0)
        self._page_spin.setMaximum(0)
        self._page_spin.setFixedWidth(64)
        self._page_spin.valueChanged.connect(self._page_spin_changed)
        main_tb.addWidget(self._page_spin)
        self._page_total = QLabel(" of 0 ", self)
        main_tb.addWidget(self._page_total)
        main_tb.addAction(self.act_next)

        # left vertical tool toolbar
        tool_tb = QToolBar("Tools", self)
        tool_tb.setObjectName("ToolToolbar")
        tool_tb.setIconSize(QSize(22, 22))
        tool_tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.addToolBar(Qt.ToolBarArea.LeftToolBarArea, tool_tb)
        tool_tb.addAction(self.act_tool_select)
        tool_tb.addAction(self.act_tool_pan)
        tool_tb.addAction(self.act_tool_object)
        tool_tb.addSeparator()
        tool_tb.addAction(self.act_tool_edit_text)
        tool_tb.addAction(self.act_tool_insert_text)
        tool_tb.addAction(self.act_tool_insert_image)
        tool_tb.addSeparator()
        tool_tb.addAction(self.act_tool_rect)
        tool_tb.addAction(self.act_tool_ellipse)
        tool_tb.addAction(self.act_tool_line)

    # ---- docks -----------------------------------------------------------
    def _create_docks(self) -> None:
        self._thumbs = ThumbnailPanel(self._document, self)
        self._outline = OutlinePanel(self._document, self)

        self._left_tabs = QTabWidget(self)
        self._left_tabs.setTabPosition(QTabWidget.TabPosition.West)
        self._left_tabs.addTab(self._thumbs, "Pages")
        self._left_tabs.addTab(self._outline, "Bookmarks")

        self._left_dock = QDockWidget("Navigation", self)
        self._left_dock.setObjectName("LeftDock")
        self._left_dock.setWidget(self._left_tabs)
        self._left_dock.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea
                                        | Qt.DockWidgetArea.RightDockWidgetArea)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self._left_dock)
        self._left_dock.resize(220, self.height())

        self._properties = PropertiesPanel(self._undo_stack.push, self)
        self._right_dock = QDockWidget("Properties", self)
        self._right_dock.setObjectName("RightDock")
        self._right_dock.setWidget(self._properties)
        self._right_dock.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea
                                         | Qt.DockWidgetArea.RightDockWidgetArea)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._right_dock)

        self._view_menu.addSeparator()
        self._view_menu.addAction(self._left_dock.toggleViewAction())
        self._view_menu.addAction(self._right_dock.toggleViewAction())

    # ---- status bar ------------------------------------------------------
    def _create_statusbar(self) -> None:
        sb = self.statusBar()
        self._status_page = QLabel("", self)
        self._status_zoom = QLabel("", self)
        sb.addPermanentWidget(self._status_page)
        sb.addPermanentWidget(self._status_zoom)

    # ---- signals ---------------------------------------------------------
    def _connect_signals(self) -> None:
        self._document.documentChanged.connect(self._on_document_changed)
        self._document.dirtyChanged.connect(lambda _d: self._update_title())
        self._document.structureChanged.connect(self._on_structure_changed)
        self._document.pageContentChanged.connect(self._on_page_content_changed)
        self._thumbs.pagesReordered.connect(self._on_pages_reordered)
        self._thumbs.contextActionRequested.connect(self._on_thumb_action)
        self._view.scene().selectionChanged.connect(self._on_scene_selection_changed)
        self._view.deleteKeyPressed.connect(self._delete_selected_objects)
        self._objects.objectChanged.connect(self._on_object_geometry_changed)
        self._view.currentPageChanged.connect(self._on_current_page_changed)
        self._watcher.fileChanged.connect(lambda _p: self._disk_timer.start())
        self._view.zoomChanged.connect(self._on_zoom_changed)
        self._thumbs.pageActivated.connect(lambda i: self._view.go_to_page(i))
        self._outline.pageRequested.connect(lambda i: self._view.go_to_page(i))
        self._search.resultsChanged.connect(self._view.set_search_results)
        self._search.gotoPage.connect(lambda i: self._view.go_to_page(i, center=True))

    # ---- open / close ----------------------------------------------------
    def open_file_dialog(self) -> None:
        start = os.path.dirname(self._recent[0]) if self._recent else os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(self, "Open PDF", start,
                                              "PDF Documents (*.pdf);;All Files (*)")
        if path:
            self.open_path(path)

    def open_path(self, path: str) -> None:
        try:
            self._document.open(path)
        except PermissionError:
            QMessageBox.warning(self, APP_NAME, "This document is password protected.")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, APP_NAME, f"Could not open file:\n{exc}")
            return
        self._add_recent(path)

    def _on_document_changed(self) -> None:
        self._undo_stack.clear()
        self._objects.clear()
        self._renderer.clear_cache()
        self._watch_current_file()
        self._view.reload()
        self._thumbs.reload()
        self._outline.reload()
        n = self._document.page_count
        self._page_spin.blockSignals(True)
        self._page_spin.setMinimum(1 if n else 0)
        self._page_spin.setMaximum(max(1, n))
        self._page_spin.setValue(1 if n else 0)
        self._page_spin.blockSignals(False)
        self._page_total.setText(f" of {n} ")
        if self._outline.has_outline():
            self._left_tabs.setCurrentIndex(1)
        else:
            self._left_tabs.setCurrentIndex(0)
        self._update_ui_state()
        self._update_title()

    def go_to_page_number(self, number: int) -> None:
        """Scroll to a 1-based page number (clamped)."""
        n = self._document.page_count
        if n:
            self._view.go_to_page(max(0, min(number - 1, n - 1)), center=False)

    # ---- follow changes made on disk ------------------------------------
    @staticmethod
    def _file_sig(path: Optional[str]):
        try:
            st = os.stat(path)
            return (st.st_mtime_ns, st.st_size)
        except (OSError, TypeError):
            return None

    def _watch_current_file(self) -> None:
        files = self._watcher.files()
        if files:
            self._watcher.removePaths(files)
        path = self._document.path
        self._disk_sig = self._file_sig(path)
        if path and os.path.exists(path):
            self._watcher.addPath(path)

    def _check_disk_change(self) -> None:
        path = self._document.path
        if not path:
            return
        if not os.path.exists(path):  # mid-replace; try again shortly
            self._disk_timer.start()
            return
        if path not in self._watcher.files():  # atomic replace drops the watch
            self._watcher.addPath(path)
        sig = self._file_sig(path)
        if sig is None or sig == self._disk_sig:
            return
        self._disk_sig = sig
        if self._document.is_dirty or self._objects.has_objects():
            self.statusBar().showMessage(
                "The file changed on disk, but you have unsaved edits — not reloaded.", 6000)
            return
        page = self._view.current_page() + 1
        try:
            with open(path + ".quill-focus") as fh:
                page = int(json.load(fh).get("page", page))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        try:
            self._document.open(path)
        except Exception as exc:  # noqa: BLE001 - e.g. a half-written file
            self.statusBar().showMessage(f"Could not reload changed file: {exc}", 6000)
            return
        self.go_to_page_number(page)
        self.statusBar().showMessage("Reloaded: the file changed on disk.", 3000)

    def close_document(self) -> None:
        if not self._maybe_discard():
            return
        self._document.close()

    # ---- object layer ----------------------------------------------------
    def _selected_objects(self) -> list:
        from .objects.editable_item import EditableItem
        return [it for it in self._view.scene().selectedItems()
                if isinstance(it, EditableItem)]

    def _on_scene_selection_changed(self) -> None:
        self._properties.set_selection(self._selected_objects())

    def _delete_selected_objects(self) -> None:
        items = self._selected_objects()
        if items:
            self._undo_stack.push(RemoveObjectsCommand(self._objects, items))

    def _on_object_geometry_changed(self, item, old_rect, new_rect) -> None:
        # Interactive move/resize already left the item at new_rect; record it
        # as an undoable step (GeometryCommand skips its first redo).
        from .objects.object_commands import GeometryCommand
        self._undo_stack.push(GeometryCommand(item, old_rect, new_rect))

    def _commit_objects(self) -> None:
        """Flatten the editable-object layer into the document before a
        structural page operation, so added content stays aligned with pages."""
        if self._objects.has_objects():
            self._objects.bake_into(self._document, clear=True)
            self.statusBar().showMessage("Added objects committed to the document.", 3000)

    def _push_page_command(self, command) -> None:
        """Commit any pending objects, then push a structural page command."""
        self._commit_objects()
        self._undo_stack.push(command)

    def _on_page_content_changed(self, index: int) -> None:
        self._renderer.invalidate_page(index)
        self._view.refresh_page(index)
        self._thumbs.refresh_page(index)
        self._update_title()

    # ---- page management -------------------------------------------------
    def _on_structure_changed(self) -> None:
        """Rebuild the views after pages were added/removed/reordered/rotated."""
        self._renderer.clear_cache()
        self._view.reload(preserve=True)
        self._thumbs.reload()
        n = self._document.page_count
        self._page_spin.blockSignals(True)
        self._page_spin.setMaximum(max(1, n))
        self._page_spin.blockSignals(False)
        self._page_total.setText(f" of {n} ")
        self._thumbs.set_current_page(self._view.current_page())

    def _target_indices(self) -> List[int]:
        """Selected thumbnails, or the current page if nothing is selected."""
        sel = self._thumbs.selected_indices()
        return sel if sel else [self._view.current_page()]

    def _on_pages_reordered(self, perm: List[int]) -> None:
        self._push_page_command(ReorderCommand(self._document, perm))

    def _on_thumb_action(self, action_id: str, indices: List[int]) -> None:
        handlers = {
            "insert_before": lambda: self._insert_blank_at(indices[0]),
            "insert_after": lambda: self._insert_blank_at(indices[-1] + 1),
            "insert_file": lambda: self._insert_from_file_at(indices[-1] + 1),
            "duplicate": lambda: self._push_page_command(
                DuplicatePageCommand(self._document, indices[0])),
            "rotate_left": lambda: self._push_page_command(
                RotateCommand(self._document, indices, -90)),
            "rotate_right": lambda: self._push_page_command(
                RotateCommand(self._document, indices, 90)),
            "extract": lambda: self._extract_indices(indices),
            "delete": lambda: self._delete_indices(indices),
        }
        handler = handlers.get(action_id)
        if handler:
            handler()

    def _insert_blank(self, before: bool) -> None:
        cur = self._view.current_page()
        self._insert_blank_at(cur if before else cur + 1)

    def _insert_blank_at(self, at: int) -> None:
        w, h = self._document.page_size_pt(min(at, self._document.page_count - 1))
        self._push_page_command(InsertBlankCommand(self._document, at, w, h))

    def _insert_from_file(self) -> None:
        self._insert_from_file_at(self._view.current_page() + 1)

    def _insert_from_file_at(self, at: int) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Insert Pages from PDF", os.path.expanduser("~"),
            "PDF Documents (*.pdf)")
        if not path:
            return
        try:
            self._push_page_command(InsertFromFileCommand(self._document, path, at))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, APP_NAME, f"Could not insert pages:\n{exc}")

    def _duplicate_current(self) -> None:
        self._push_page_command(DuplicatePageCommand(self._document, self._view.current_page()))

    def _rotate_current(self, delta: int) -> None:
        self._push_page_command(RotateCommand(self._document, self._target_indices(), delta))

    def _delete_current(self) -> None:
        self._delete_indices(self._target_indices())

    def _delete_indices(self, indices: List[int]) -> None:
        if len(indices) >= self._document.page_count:
            QMessageBox.warning(self, APP_NAME, "Cannot delete every page.")
            return
        self._push_page_command(DeletePagesCommand(self._document, indices))

    def _extract_pages(self) -> None:
        self._extract_indices(self._target_indices())

    def _extract_indices(self, indices: List[int]) -> None:
        default = os.path.join(os.path.expanduser("~"), "extracted.pdf")
        path, _ = QFileDialog.getSaveFileName(self, "Extract Pages To", default,
                                              "PDF Documents (*.pdf)")
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        try:
            self._document.export_pages(indices, path)
            self.statusBar().showMessage(f"Extracted {len(indices)} page(s).", 3000)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, APP_NAME, f"Could not extract pages:\n{exc}")

    def _split_document(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Choose Folder for Split Pages", os.path.expanduser("~"))
        if not folder:
            return
        stem = os.path.splitext(self._document.display_name())[0] or "page"
        try:
            written = self._document.split_to_folder(folder, stem)
            self.statusBar().showMessage(f"Wrote {len(written)} files to {folder}.", 4000)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, APP_NAME, f"Could not split document:\n{exc}")

    # ---- save ------------------------------------------------------------
    def save(self) -> None:
        if not self._document.is_open:
            return
        if not self._document.path:
            self.save_as()
            return
        try:
            self._document.save(bake_fn=self._bake_fn())
            self._disk_sig = self._file_sig(self._document.path)
            self.statusBar().showMessage("Saved.", 3000)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, APP_NAME, f"Could not save:\n{exc}")

    def _bake_fn(self):
        """A callable that bakes the live object layer into a fitz.Document,
        or None when there is nothing to bake (so we can save incrementally)."""
        if not self._objects.has_objects():
            return None
        items = self._objects.items()

        def bake(clone):
            for it in items:
                if 0 <= it.page_index < clone.page_count:
                    it.bake(clone.load_page(it.page_index))
        return bake

    def save_as(self) -> None:
        if not self._document.is_open:
            return
        start = self._document.path or os.path.expanduser("~/untitled.pdf")
        path, _ = QFileDialog.getSaveFileName(self, "Save PDF As", start,
                                              "PDF Documents (*.pdf)")
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        try:
            self._document.save_as(path, bake_fn=self._bake_fn())
            self._watch_current_file()
            self._add_recent(path)
            self._update_title()
            self.statusBar().showMessage("Saved.", 3000)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, APP_NAME, f"Could not save:\n{exc}")

    # ---- print -----------------------------------------------------------
    def print_document(self) -> None:
        if not self._document.is_open:
            return
        from PySide6.QtPrintSupport import QPrinter, QPrintDialog
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        if QPrintDialog(printer, self).exec() != QPrintDialog.DialogCode.Accepted:
            return
        painter = QPainter(printer)
        page_rect = printer.pageRect(QPrinter.Unit.DevicePixel)
        dpi = printer.resolution()
        scale = dpi / 72.0
        doc = self._document.doc
        for i in range(self._document.page_count):
            if i > 0:
                printer.newPage()
            with QMutexLocker(self._document.lock):
                page = doc.load_page(i)
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                from PySide6.QtGui import QImage
                img = QImage(pix.samples, pix.width, pix.height, pix.stride,
                             QImage.Format.Format_RGB888).copy()
            fit = min(page_rect.width() / img.width(), page_rect.height() / img.height())
            w, h = img.width() * fit, img.height() * fit
            x = page_rect.x() + (page_rect.width() - w) / 2
            y = page_rect.y() + (page_rect.height() - h) / 2
            from PySide6.QtCore import QRectF
            painter.drawImage(QRectF(x, y, w, h), img)
        painter.end()
        self.statusBar().showMessage("Sent to printer.", 3000)

    # ---- edit ------------------------------------------------------------
    def copy_selection(self) -> None:
        tool = self._view.current_tool()
        text = tool.selected_text() if isinstance(tool, SelectTextTool) else ""
        if text:
            QGuiApplication.clipboard().setText(text)
            self.statusBar().showMessage("Copied selection.", 2000)
        else:
            self.statusBar().showMessage("No text selected.", 2000)

    def show_find(self) -> None:
        if not self._document.is_open:
            return
        self._search.show()
        self._search.focus_input()

    # ---- navigation glue -------------------------------------------------
    def _go_prev(self) -> None:
        self._view.go_to_page(self._view.current_page() - 1, center=False)

    def _go_next(self) -> None:
        self._view.go_to_page(self._view.current_page() + 1, center=False)

    def _page_spin_changed(self, value: int) -> None:
        if value >= 1:
            self._view.go_to_page(value - 1, center=False)

    def _on_current_page_changed(self, index: int) -> None:
        self._page_spin.blockSignals(True)
        self._page_spin.setValue(index + 1)
        self._page_spin.blockSignals(False)
        self._thumbs.set_current_page(index)
        self._status_page.setText(f" Page {index + 1} / {self._document.page_count} ")

    # ---- zoom glue -------------------------------------------------------
    def _on_zoom_changed(self, zoom: float) -> None:
        pct = f"{int(round(zoom * 100))}%"
        self._zoom_combo.blockSignals(True)
        self._zoom_combo.setEditText(pct)
        self._zoom_combo.blockSignals(False)
        self._status_zoom.setText(f" {pct} ")

    def _zoom_combo_activated(self, index: int) -> None:
        data = self._zoom_combo.itemData(index)
        if data is not None:
            self._view.set_zoom(float(data))

    def _zoom_combo_entered(self) -> None:
        text = self._zoom_combo.currentText().strip().rstrip("%")
        try:
            self._view.set_zoom(float(text) / 100.0)
        except ValueError:
            self._on_zoom_changed(self._view.zoom())

    # ---- recent files ----------------------------------------------------
    def _add_recent(self, path: str) -> None:
        path = os.path.abspath(path)
        if path in self._recent:
            self._recent.remove(path)
        self._recent.insert(0, path)
        del self._recent[MAX_RECENT:]
        self._settings.setValue("recentFiles", self._recent)
        self._rebuild_recent_menu()

    def _rebuild_recent_menu(self) -> None:
        self._recent_menu.clear()
        if not self._recent:
            empty = self._recent_menu.addAction("(none)")
            empty.setEnabled(False)
            return
        for path in self._recent:
            act = self._recent_menu.addAction(os.path.basename(path))
            act.setToolTip(path)
            act.triggered.connect(lambda _c=False, p=path: self.open_path(p))
        self._recent_menu.addSeparator()
        clear = self._recent_menu.addAction("Clear List")
        clear.triggered.connect(self._clear_recent)

    def _clear_recent(self) -> None:
        self._recent = []
        self._settings.setValue("recentFiles", self._recent)
        self._rebuild_recent_menu()

    # ---- misc UI ---------------------------------------------------------
    def _update_ui_state(self) -> None:
        has = self._document.is_open
        for act in (self.act_save, self.act_save_as, self.act_print, self.act_close,
                    self.act_copy, self.act_find, self.act_zoom_in, self.act_zoom_out,
                    self.act_fit_width, self.act_fit_page, self.act_actual_size,
                    self.act_prev, self.act_next,
                    self.act_insert_before, self.act_insert_after, self.act_insert_file,
                    self.act_duplicate, self.act_delete_page, self.act_rotate_left,
                    self.act_rotate_right, self.act_extract, self.act_split,
                    self.act_tool_object, self.act_tool_edit_text,
                    self.act_tool_insert_text, self.act_tool_insert_image,
                    self.act_tool_rect, self.act_tool_ellipse, self.act_tool_line):
            act.setEnabled(has)
        self._zoom_combo.setEnabled(has)
        self._page_spin.setEnabled(has)

    def _update_title(self) -> None:
        if self._document.is_open:
            mark = "•" if self._document.is_dirty else ""
            self.setWindowTitle(f"{mark}{self._document.display_name()} — {APP_NAME}")
        else:
            self.setWindowTitle(APP_NAME)

    def show_about(self) -> None:
        QMessageBox.about(
            self, f"About {APP_NAME}",
            f"<h3>{APP_NAME} {__version__}</h3>"
            "<p>An original PDF editor built with PySide6 and PyMuPDF.</p>"
            "<p>UI workflow inspired by Master PDF Editor; independently implemented.</p>")

    # ---- geometry persistence -------------------------------------------
    def _restore_geometry(self) -> None:
        geo = self._settings.value("windowGeometry")
        if geo is not None:
            self.restoreGeometry(geo)
        state = self._settings.value("windowState")
        if state is not None:
            self.restoreState(state)

    def _maybe_discard(self) -> bool:
        if not self._document.is_dirty:
            return True
        ret = QMessageBox.question(
            self, APP_NAME, "This document has unsaved changes. Save before closing?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel)
        if ret == QMessageBox.StandardButton.Save:
            self.save()
            return not self._document.is_dirty
        return ret == QMessageBox.StandardButton.Discard

    def closeEvent(self, event) -> None:
        if not self._maybe_discard():
            event.ignore()
            return
        self._settings.setValue("windowGeometry", self.saveGeometry())
        self._settings.setValue("windowState", self.saveState())
        super().closeEvent(event)
