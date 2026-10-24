# CLAUDE.md

Quill PDF Editor: a PySide6 + PyMuPDF desktop PDF editor with an MCP server
(`quill-pdf`) that lets agents edit PDFs.

## When the user asks you to view or edit a PDF

Use the `quill-pdf` MCP tools (not ad-hoc PyMuPDF scripts) so edits are
style-preserving, undoable and verifiable:

1. `open_pdf(path)`.
2. Look first: `render_page(page)`. Add `overlay=true` to see element ids, and
   `grid=true` when placing new content. Read `get_page_layout(page)` for ids,
   boxes and styles. `find_text` locates text and returns usable line/block ids.
3. Edit with ids from the latest layout of that page. Any edit to a page
   invalidates its ids, so re-read the layout before the next id-based edit there.
4. Verify every meaningful change with `render_page`, and `undo` if it's wrong.
   Act on warnings (e.g. "font shrunk", "line grew wider").
5. `save_pdf(output_path=...)` unless the user asked to overwrite.
   Overwriting makes a one-time `<name>.orig.pdf` backup.
6. If the user wants to watch, call `open_in_viewer`.

Prefer `delete_elements`/`delete_region` (real removal) over covering content
with white shapes, and `move_elements`/`move_region` for re-layout. The full
reference is in `docs/MCP_TOOLS.md`; the user guide is `docs/USING_WITH_CLAUDE.md`.

## Developing

- Run the app: `./run.sh [file.pdf] [--page N]`. Run the MCP server: `./mcp-server.sh`.
- Layout: `pdfeditor/ui` (Qt GUI), `pdfeditor/document` (GUI document model and
  undo commands), `pdfeditor/agent` (Qt-free engine, renderer, MCP server).
  Shared helpers: `pdfeditor/fonts.py`, `pdfeditor/textfit.py`.
- Tests are plain scripts (no pytest in the venv):
  ```bash
  .venv/bin/python tests/test_agent_engine.py
  .venv/bin/python tests/test_mcp_e2e.py
  QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_gui_follow.py
  ```
- After changing MCP tools, regenerate the reference:
  `.venv/bin/python -m pdfeditor.agent.gen_docs`.
- The MCP SDK is v2 (`mcp.server.mcpserver.MCPServer`; `ToolAnnotations` uses
  snake_case fields).

PyMuPDF gotchas this code relies on:
- `apply_redactions` applies *all* redaction annots on a page and drops fonts
  that become unused. Build a `FontResolver` before redacting.
- A `Widget` stops working once its `Page` object is garbage-collected, so keep
  page references alive while editing fields.
- `insert_textbox` writes nothing when text overflows; use the fit helpers.
- Layout/edit coordinates are in unrotated page space; the renderer maps
  them through `page.rotation_matrix`.
- In the GUI, `QRecursiveMutex` isn't a context manager; use
  `QMutexLocker(doc.lock)`.
