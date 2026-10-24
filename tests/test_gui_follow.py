"""GUI test (offscreen): the main window follows external changes to its file —
the mechanism the MCP server's open_in_viewer relies on — and never reloads
over the user's own unsaved edits or its own saves.

Run:  QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_gui_follow.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication  # noqa: E402

from fixtures import build_fixture  # noqa: E402
from pdfeditor.agent.engine import AgentDocument  # noqa: E402
from pdfeditor.ui.main_window import MainWindow  # noqa: E402


def pump(app, seconds=0.8):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def write_like_agent(adoc: AgentDocument, path: str, focus_page: int) -> None:
    with open(path + ".quill-focus", "w") as fh:
        json.dump({"page": focus_page}, fh)
    adoc.doc.save(path + ".tmp", garbage=1)
    os.replace(path + ".tmp", path)


def main() -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    tmp = tempfile.mkdtemp(prefix="quill-gui-")
    path = build_fixture(os.path.join(tmp, "live.pdf"))
    w = MainWindow()
    w.show()
    w.open_path(path)
    pump(app, 0.3)
    doc = w._document
    assert doc.page_count == 3

    # 1) external edit (atomic replace) -> reload + jump to the focus page
    agent = AgentDocument(path)
    agent.insert_blank_page(4)
    agent.add_text(4, [72, 72, 400, 120], "Added by the agent")
    write_like_agent(agent, path, 4)
    pump(app)
    assert doc.page_count == 4, doc.page_count
    assert "Added by the agent" in doc.page_text(3)
    assert w._view.current_page() == 3, w._view.current_page()

    # 2) the watch survives the replace: a second change is also picked up
    agent.delete_pages([4])
    write_like_agent(agent, path, 1)
    pump(app)
    assert doc.page_count == 3

    # 3) unsaved local edits are never clobbered
    doc.set_dirty(True)
    agent.insert_blank_page(1)
    write_like_agent(agent, path, 1)
    pump(app)
    assert doc.page_count == 3, "reloaded over unsaved edits"
    doc.set_dirty(False)

    # 4) the window's own save doesn't trigger a reload loop
    w.open_path(path)
    pump(app, 0.3)
    n = doc.page_count
    doc.set_dirty(True)
    w.save()
    pump(app)
    assert doc.page_count == n and not doc.is_dirty

    doc.set_dirty(False)
    w.close()
    print("PASS GUI follows external changes")


if __name__ == "__main__":
    main()
