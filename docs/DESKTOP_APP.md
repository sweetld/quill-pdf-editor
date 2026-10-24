# Quill PDF Editor — desktop app guide

Quill is a desktop PDF editor for Linux built with PySide6 (Qt 6) and PyMuPDF.
Its layout follows the familiar Master PDF Editor arrangement: menu bar and
toolbar at the top, editing tools on the left, a tabbed Pages/Bookmarks panel,
the continuous document view, and a Properties panel for the selected object.

To have Claude make the edits for you instead, see
[USING_WITH_CLAUDE.md](USING_WITH_CLAUDE.md).

## Install and run

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt

./run.sh                          # start empty
./run.sh path/to/file.pdf         # open a file
./run.sh path/to/file.pdf --page 5  # open at page 5
```

Requires Python 3.10+. Developed and tested on Pop!_OS 22.04.

## Viewing

- **Open:** File › Open (Ctrl+O), File › Open Recent, or pass a file on the command line.
- **Scroll** through pages continuously. Go to a page with the page box in the
  toolbar, PgUp/PgDn, the **Pages** thumbnails or the **Bookmarks** outline.
- **Zoom:** Ctrl+ / Ctrl−, Ctrl+scroll wheel, Actual Size (Ctrl+0),
  Fit Width (Ctrl+1), Fit Page (Ctrl+2) or type a percentage.
- **Find:** Ctrl+F opens the search bar. Matches are highlighted; step through
  them with next/previous.
- **Select & copy text:** with the Select Text tool, drag over words, then Ctrl+C.
- **Print:** Ctrl+P.

## Managing pages

Use the **Document** menu, or right-click thumbnails in the **Pages** panel
(select several with Ctrl/Shift-click):

- Insert a blank page before/after, or insert pages from another PDF
- Duplicate, rotate left/right (Ctrl+Shift+← / →), delete
- Reorder by dragging thumbnails
- Extract selected pages to a new PDF, or split the document into single pages

Every page operation can be undone (Ctrl+Z) and redone (Ctrl+Shift+Z).

## Editing content (Tools menu / left toolbar)

| Tool | How to use |
| --- | --- |
| **Select Text** | Drag to select words; Ctrl+C copies. |
| **Hand / Pan** | Drag to scroll the page. |
| **Edit Objects** | Click an object to select it, then drag to move, use the 8 handles to resize, or press Delete. **Click existing page content** (a text line, an image or a vector drawing) to *lift* it into an editable object. Its original is removed from the page and replaced by an object you can move, resize and restyle. Double-click a text object to edit its text. |
| **Edit Text** | Click a text block to edit its wording in place. The original font size, colour and bold/italic are kept. |
| **Insert Text Box** | Drag a rectangle, then type. |
| **Insert Image** | Drag a rectangle, then choose an image file. |
| **Rectangle / Ellipse / Line** | Drag to draw. |

The **Properties** panel shows the selected object's settings: text, font
size, bold/italic and colour for text; stroke, line width and fill for shapes.

Added and lifted objects stay editable until you save. Saving writes them into
the PDF. Before a page operation (insert/delete/rotate…) they are committed to
the page first.

## Saving

- **Save** (Ctrl+S) writes back to the open file; **Save As** (Ctrl+Shift+S)
  writes a new file.
- Quill asks before closing a document with unsaved changes.

## Live reload

If the open file is changed on disk by another program (for example when
Claude edits it, or the `open_in_viewer` live preview), Quill reloads it
automatically and keeps your place. It never reloads over unsaved edits you've
made; the status bar tells you instead.

## Keyboard shortcuts

| Action | Shortcut |
| --- | --- |
| Open | Ctrl+O |
| Save / Save As | Ctrl+S / Ctrl+Shift+S |
| Print | Ctrl+P |
| Close / Quit | Ctrl+W / Ctrl+Q |
| Undo / Redo | Ctrl+Z / Ctrl+Shift+Z |
| Copy | Ctrl+C |
| Find | Ctrl+F |
| Zoom In / Out | Ctrl++ / Ctrl+- |
| Actual Size | Ctrl+0 |
| Fit Width / Page | Ctrl+1 / Ctrl+2 |
| Previous / Next page | PgUp / PgDn |
| Rotate page left / right | Ctrl+Shift+← / Ctrl+Shift+→ |
| Delete selected object | Delete or Backspace |

## Not yet available

- Forms in the desktop app (filling and creating fields) is the next phase.
  Claude can already do this through the MCP tools.
- Annotation tools (highlight, notes, ink, stamps) are planned. Claude can
  already add basic annotations.
- Preferences, a `.desktop` launcher and AppImage packaging are planned.
