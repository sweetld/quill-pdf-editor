"""Application bootstrap."""
from __future__ import annotations

import sys
from typing import List, Optional

from PySide6.QtWidgets import QApplication

from . import APP_NAME, ORG_NAME
from .ui.main_window import MainWindow


def run(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setApplicationDisplayName(APP_NAME)

    window = MainWindow()
    window.show()

    # Open a file passed on the command line (optionally: --page N).
    args = argv[1:]
    page = None
    if "--page" in args:
        i = args.index("--page")
        try:
            page = int(args[i + 1])
        except (IndexError, ValueError):
            pass
        del args[i:i + 2]
    for arg in args:
        if arg.lower().endswith(".pdf"):
            window.open_path(arg)
            if page:
                window.go_to_page_number(page)
            break

    return app.exec()
