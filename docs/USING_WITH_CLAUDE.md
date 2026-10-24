# Editing PDFs with Claude (or any AI agent)

Quill includes an [MCP](https://modelcontextprotocol.io) server, `quill-pdf`, that
lets Claude open an existing PDF, look at its pages, change them and save the
result. You describe the change in plain English; Claude uses Quill's editing
engine to do it and checks its own work by looking at the rendered pages.

- [1. Set up](#1-set-up)
- [2. Asking Claude to edit a PDF](#2-asking-claude-to-edit-a-pdf)
- [3. Watching the edits live](#3-watching-the-edits-live)
- [4. What Claude can do](#4-what-claude-can-do)
- [5. How it works (and why it's safe)](#5-how-it-works-and-why-its-safe)
- [6. Limitations](#6-limitations)
- [7. Troubleshooting](#7-troubleshooting)
- [8. For developers](#8-for-developers)

The full tool-by-tool reference is in [MCP_TOOLS.md](MCP_TOOLS.md).

---

## 1. Set up

### Install

From the project folder (once):

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
```

The requirements include the `mcp` package (version 2 or later). The server is
started by [`mcp-server.sh`](../mcp-server.sh), which runs
`python -m pdfeditor.agent` from the project's virtualenv over stdio.

### Register the server with your client

**Claude Code, inside this project.** The repo's [`.mcp.json`](../.mcp.json)
already registers `quill-pdf`. Run `claude` in the project folder and approve
the server when asked (or later with `/mcp`). `.mcp.json` contains this
machine's absolute path; if you clone the repo elsewhere, edit the path.

**Claude Code, from any folder** (recommended if you keep PDFs elsewhere):

```bash
claude mcp add --scope user quill-pdf -- /media/daud/Hybrid640/work/master-pdf-editor/mcp-server.sh
```

Check it with `claude mcp get quill-pdf` or `/mcp` inside a session. To
remove it: `claude mcp remove quill-pdf -s user`.

**Claude Desktop or another MCP client.** Add this to the client's MCP
configuration (for Claude Desktop on Linux: `~/.config/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "quill-pdf": {
      "command": "/media/daud/Hybrid640/work/master-pdf-editor/mcp-server.sh"
    }
  }
}
```

Restart the client afterwards.

---

## 2. Asking Claude to edit a PDF

Just say what you want, including the file path and where to save. Claude
opens the file, looks at the pages, makes the changes, checks the result
visually and saves.

Example requests:

> Open `~/Documents/application.pdf`. Change the fees to £130, £65 and £210,
> delete the "Office use only" line, and save it as `application-2027.pdf`.

> In `~/contract.pdf`, replace every "Acme Ltd" with "Acme Group Ltd" and show
> me the pages that changed before saving.

> Move the signature block on page 3 to the bottom of the page, and delete the
> blank page 4.

> Add fillable text fields for Name, Date of birth, Address and Telephone to
> the table on page 1, then fill in Name with "Jane Doe".

> Re-layout page 2: put the two contact boxes side by side instead of stacked.

> Rotate pages 5–7 to landscape, merge in `~/appendix.pdf` at the end and
> flatten the form so it can't be edited.

Tips for good results:

- **Say where to save.** "Save as …" writes a new file and leaves the original
  untouched. "Save it" overwrites the original, but Quill first keeps a
  one-time backup called `<name>.orig.pdf`.
- **Ask to see it.** "Show me page 2 before and after" makes Claude render and
  show you the pages.
- **Iterate.** "Make the heading a bit smaller", "undo that", "move it 20
  points left" all work. Every edit can be undone until you close the session.
- **Be specific about which text.** For example, "the second 'Total' in the
  table on page 3" rather than "the total".

---

## 3. Watching the edits live

Ask Claude to **"open it in the viewer"**. Quill's desktop window opens on a
live preview copy of the document and then reloads after every edit, jumping
to the page that changed, so you can watch the work as it happens.

- The preview copy lives in `~/.cache/quill-pdf/live/`. Your original file is
  only written when Claude saves.
- Edits you make yourself in that preview window go to the preview copy, not
  your file, and are replaced by Claude's next change. To make your own changes,
  have Claude save first, then open the saved file in Quill normally.
- Any Quill window reloads automatically when its open file changes on disk.
  It never reloads over unsaved edits you've made in that window.

---

## 4. What Claude can do

| Area | What's possible |
| --- | --- |
| **Look** | Render any page or a zoomed region as an image; overlay every element's id and box; add a coordinate grid; list the structure of a page (text with font/size/colour, images, vector drawings, form fields, annotations); search text |
| **Delete** | Specific elements (a line, a paragraph, an image, a box, a field) or everything inside a rectangle. Content is truly removed; no white boxes are painted over it |
| **Change text** | Rewrite a line or paragraph keeping its font, size and colour (the box grows into empty space if the new text is longer); reflow text into a different area; find & replace across the document keeping each word's styling |
| **Add** | Plain text, rich text from HTML/CSS (headings, bold/italic, lists, simple tables), images, rectangles/ellipses/lines, highlight/underline/strike-out/note/comment annotations |
| **Re-layout** | Move, copy or scale any set of elements or a whole region, on the same page or onto another page. Moved content keeps its exact fonts and vector quality, and form fields inside the region move too |
| **Forms** | List, fill, add, rename, move/resize, change options or flags, delete and flatten form fields |
| **Pages** | Delete, reorder, move, insert blank, duplicate, rotate, crop, insert pages from another PDF, extract pages to a new file |
| **Safety** | Undo/redo (40 steps), nothing written until save, backup before overwriting |

---

## 5. How it works (and why it's safe)

**Everything happens in memory.** `open_pdf` loads a working copy. Edits change
the copy; `save_pdf` writes it. Before every edit Quill snapshots the document,
so `undo`/`redo` restore it exactly, and a failing edit is rolled back
automatically.

**Claude sees what it edits.** `render_page` returns a PNG of the current
working copy. With `overlay=true` every text block, image, drawing, field and
annotation is boxed and labelled with its id; with `grid=true` coordinate lines
help place new content precisely.

**Element ids.** `get_page_layout` names everything on a page:

| Id | Meaning |
| --- | --- |
| `t3` | text block 3 (blocks are numbered in reading order) |
| `t3.1` | line 1 of block 3 (table rows often appear as one block with one line per cell) |
| `i0` | image |
| `g5` | vector drawing (line, box, path); page-sized backgrounds are marked `background` |
| `w2` | form field (widget) |
| `a1` | annotation |

Ids are only valid until that page is edited. If Claude tries to use an id from
before the change, the edit is refused with a clear "out of date" message
instead of hitting the wrong thing; Claude then re-reads the page.

**Coordinates** are PDF points (1/72 inch), origin at the top-left, y going
down, in the page's unrotated space. Pages are numbered from 1.

**Deleting really deletes.** Removal uses PDF redaction with no fill, so text,
images and paths are taken out of the page content rather than covered with a
white rectangle. Removed text can't be recovered by selecting or copying.

**Fonts are preserved.** Re-written text uses, in order:
1. the font embedded in the PDF, if it contains every character needed;
2. the same font installed on this computer, or a metric-compatible substitute
   (Arial → Liberation Sans, Times New Roman → Liberation Serif,
   Calibri → Carlito, Cambria → Caladea, Courier New → Liberation Mono);
3. the closest standard PDF font (Helvetica/Times/Courier, bold/italic),
   which always renders.

Fonts added this way are subset on save, so files stay small.

**Moving keeps fidelity.** Moved regions are copied as vector content (a form
XObject) with their original fonts, so they look identical and remain
searchable and editable afterwards.

---

## 6. Limitations

- **Find & replace works within single lines.** A phrase broken across two
  lines isn't matched. Justified lines are re-set with normal word spacing.
- **Changing bold/italic** on text whose own font has no such variant uses a
  standard PDF font (Helvetica/Times/Courier).
- **Scanned PDFs** are images of text. They can be moved, deleted and covered,
  but their words can't be edited (no OCR).
- **Radio buttons** can't be moved or copied to a different page.
- **Pending redaction marks** made by another tool block edits on that page.
  Claude will ask to delete those marks first rather than silently apply them.
- **Password-protected PDFs** need the password (`open_pdf(path, password=…)`).
- **Vertical/rotated text** is skipped by find & replace (it's reported).

---

## 7. Troubleshooting

| Problem | Fix |
| --- | --- |
| Claude doesn't have the PDF tools | Run `/mcp` in Claude Code. Approve `quill-pdf` if it's pending, or register it (section 1). Check `claude mcp get quill-pdf`. |
| Server fails to start | Run `./mcp-server.sh` in a terminal; it should wait silently for input (Ctrl+C to stop). If it errors, reinstall: `./.venv/bin/python -m pip install -r requirements.txt`. |
| "Element ids … are out of date" | Normal: the page changed. Claude re-reads the layout and retries. |
| "Text did not fit; font shrunk" | Ask Claude to give the text a larger area (or a smaller font), or shorten the text. |
| Viewer doesn't open | The server needs a graphical display. It looks for one automatically; if none is found, run Claude from a desktop terminal. |
| Viewer shows old content | It only reloads when it has no unsaved edits of its own. Close it and ask Claude to open it again. |
| Unwanted `.orig.pdf` next to a file | That is the one-time backup of the original before it was overwritten. Delete it once you're happy, or ask Claude to save with `backup=false`. |

---

## 8. For developers

| Path | Purpose |
| --- | --- |
| `pdfeditor/agent/engine.py` | `AgentDocument`: headless (Qt-free) editing engine, layout model, undo snapshots, font resolution |
| `pdfeditor/agent/render.py` | Page → PNG with id overlay and coordinate grid |
| `pdfeditor/agent/server.py` | MCP server: tool definitions, open documents, live viewer |
| `pdfeditor/agent/gen_docs.py` | Regenerates [MCP_TOOLS.md](MCP_TOOLS.md) from the tool registry |
| `mcp-server.sh`, `.mcp.json` | Launcher and Claude Code project registration |
| `tests/` | `test_agent_engine.py` (engine), `test_mcp_e2e.py` (real stdio MCP session), `test_gui_follow.py` (viewer live-reload) |

The engine can also be used directly from Python:

```python
from pdfeditor.agent import AgentDocument

doc = AgentDocument("in.pdf")
doc.replace_text("Acme Ltd", "Acme Group Ltd")
line = doc.find_text("DRAFT")[0]          # also loads that page's ids
doc.delete_elements(line["page"], [line["line_id"]])
doc.save("out.pdf")
```

After adding or changing a tool, regenerate the reference and run the tests:

```bash
.venv/bin/python -m pdfeditor.agent.gen_docs
.venv/bin/python tests/test_agent_engine.py
.venv/bin/python tests/test_mcp_e2e.py
QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_gui_follow.py
```
