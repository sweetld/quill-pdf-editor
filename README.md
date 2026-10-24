# Quill PDF Editor

An original desktop PDF editor for Linux, built with **PySide6 (Qt6)** and
**PyMuPDF**. Its UI layout and workflow follow the familiar Master PDF Editor
arrangement (top menu + toolbar, left vertical tool toolbar, tabbed navigation
dock, continuous document view, status bar), but it is an independent
implementation with original code and icons.

Quill can also be **driven by AI agents**. Its MCP server lets Claude open a
PDF, see the pages, delete sections, rewrite text, re-lay-out content and forms,
and save the result, all from a plain-English request.

## Documentation

| Guide | For |
| --- | --- |
| [docs/USING_WITH_CLAUDE.md](docs/USING_WITH_CLAUDE.md) | Setting up and using Quill with Claude / MCP clients: example requests, live viewing, safety, troubleshooting |
| [docs/MCP_TOOLS.md](docs/MCP_TOOLS.md) | Reference for every MCP tool and parameter (generated) |
| [docs/DESKTOP_APP.md](docs/DESKTOP_APP.md) | Using the desktop app: viewing, page management, editing tools, shortcuts |
| [CLAUDE.md](CLAUDE.md) | Notes for AI agents working on or with this repo |

## Quick start

Requires Python 3.10+ on Linux (developed/tested on Pop!_OS 22.04).

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt

./run.sh                     # desktop app (or: ./.venv/bin/python -m pdfeditor)
./run.sh path/to/file.pdf    # open a file (add --page N to jump to a page)
```

### Edit PDFs with Claude

Register the MCP server once:

```bash
# Claude Code, available in every folder
claude mcp add --scope user quill-pdf -- "$PWD/mcp-server.sh"
```

Inside this repo, `.mcp.json` already registers it; just approve `quill-pdf`
when Claude Code asks. For Claude Desktop and other clients, see the
[guide](docs/USING_WITH_CLAUDE.md#1-set-up).

Then ask, for example:

> Open `~/forms/application.pdf`, change the fees to £130/£65/£210, delete the
> "Office use only" line, add fillable fields to the personal-details table and
> save it as `application-2027.pdf`. Open it in the viewer so I can watch.

Claude looks at each page, makes the edits with Quill's engine and checks the
result visually. Nothing is written until it saves, and every edit can be undone.

## Status

Built in phases:

1. ✅ **Viewer** — continuous scrolling, zoom/fit, thumbnails, bookmarks, text
   selection and copy, find, print, save.
2. ✅ **Page management** — insert, delete, duplicate, rotate, reorder (drag),
   insert from file, extract, split. Fully undoable.
3. ✅ **Content editing** — edit existing text keeping its style; insert text,
   images and shapes; lift existing text/images/drawings into movable,
   resizable objects.
   - ✅ **Agent interface** — MCP server with 38 tools (inspect, render, delete,
     edit/replace text, move/scale regions, forms, pages, undo, save, live viewer).
4. ⏳ **Forms in the desktop app** — fill and create fields (already available
   to agents via MCP).
5. Annotations & comments — highlight, notes, ink, stamps.
6. Polish & packaging — preferences, `.desktop` launcher, AppImage.

## Tests

```bash
.venv/bin/python tests/test_agent_engine.py                          # editing engine
.venv/bin/python tests/test_mcp_e2e.py                               # real MCP session over stdio
QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_gui_follow.py  # viewer live-reload
```

## Project layout

```
pdfeditor/
  ui/          Qt GUI: main window, document view, tools, panels, editable objects
  document/    GUI document model (PdfDocument) and undo commands
  render/      async page renderer
  agent/       Qt-free editing engine, page renderer and MCP server
  fonts.py, textfit.py   shared font/text-fitting helpers
docs/          user guides and the MCP tool reference
tests/         test scripts and fixture builder
run.sh, mcp-server.sh, .mcp.json
```
