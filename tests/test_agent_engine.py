"""Tests for the headless agent engine (pdfeditor.agent).

Run:  .venv/bin/python tests/test_agent_engine.py      (or with pytest)
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback

import fitz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fixtures import build_fixture  # noqa: E402
from pdfeditor.agent.engine import AgentDocument, EngineError, layout_to_dict  # noqa: E402
from pdfeditor.agent.render import render_page  # noqa: E402

TMP = tempfile.mkdtemp(prefix="quill-agent-test-")


def fresh(name="fixture.pdf") -> AgentDocument:
    path = os.path.join(TMP, name)
    build_fixture(path)
    return AgentDocument(path)


def find_line(adoc, page, text):
    lay = adoc.layout(page)
    for e in lay.of_kind("line"):
        if text in e.lines[0].text:
            return e
    raise AssertionError(f"line containing {text!r} not found on page {page}")


def page_text(adoc, page):
    return adoc.doc[page - 1].get_text()


def pixel(adoc, page, x, y):
    pix = adoc.doc[page - 1].get_pixmap(clip=fitz.Rect(x, y, x + 1, y + 1))
    return pix.pixel(0, 0)


# ---- inspection -------------------------------------------------------------
def test_open_and_layout():
    a = fresh()
    info = a.info()
    assert info["pages"] == 3 and info["form_fields"] == 4 and info["rotated_pages"] == [3]
    d = layout_to_dict(a.layout(1))
    texts = [t["text"] for t in d["text"]]
    assert any("Quarterly Report" in t for t in texts)
    heading = next(t for t in d["text"] if "Quarterly" in t["text"])
    assert heading["size"] == 22 and heading["color"] == "#1a3380"
    assert len(d["images"]) == 1 and len(d["drawings"]) >= 8
    para = next(t for t in d["text"] if t["text"].startswith("Revenue grew"))
    assert len(para["lines"]) >= 2, para
    d2 = layout_to_dict(a.layout(2))
    assert {f["name"] for f in d2["fields"]} == {"name", "email", "country", "subscribe"}
    assert any(g.get("background") for g in d2["drawings"])
    region = layout_to_dict(a.layout(1), kinds=["text"], region=fitz.Rect(72, 230, 300, 330))
    assert [t["text"] for t in region["text"]] == ["Contact: Alice", "Phone: 555-0100"], region


def test_find_text_gives_usable_ids():
    a = fresh()
    hits = a.find_text("alice")
    assert len(hits) == 1 and hits[0]["page"] == 1
    a.delete_elements(1, [hits[0]["line_id"]])
    assert "Alice" not in page_text(a, 1)


def test_stale_ids_are_rejected():
    a = fresh()
    draft = find_line(a, 1, "DRAFT")
    other = find_line(a, 1, "Page 1 footer")
    a.delete_elements(1, [draft.id])
    try:
        a.delete_elements(1, [other.id])
    except EngineError as exc:
        assert "out of date" in str(exc)
    else:
        raise AssertionError("stale id was accepted")
    # page 2 is untouched, so its ids remain valid
    a.layout(2)
    a.delete_elements(1, [find_line(a, 1, "footer").id])
    assert "footer" not in page_text(a, 1)


# ---- deletion ---------------------------------------------------------------
def test_delete_line_leaves_no_box_and_keeps_neighbours():
    a = fresh()
    n_draw = len(a.doc[0].get_drawings())
    draft = find_line(a, 1, "DRAFT")
    a.delete_elements(1, [draft.id])
    txt = page_text(a, 1)
    assert "DRAFT" not in txt
    assert "ahead of schedule" in txt and "Contact: Alice" in txt
    assert len(a.doc[0].get_drawings()) == n_draw  # no white rectangles added
    assert a.doc[0].first_annot is None


def test_delete_paragraph_line_keeps_adjacent_lines():
    a = fresh()
    lay = a.layout(1)
    para = next(e for e in lay.of_kind("text") if e.lines[0].text.startswith("Revenue"))
    before = [tl.text for tl in para.lines]
    a.delete_elements(1, [para.lines[1].id])
    txt = page_text(a, 1)
    assert before[1].strip() not in txt
    assert len(before) >= 2 and before[0].strip() in txt


def test_delete_image_drawing_and_region():
    a = fresh()
    lay = a.layout(1)
    img = lay.of_kind("image")[0]
    panel = next(e for e in lay.of_kind("drawing") if e.info["fill"] == "#ffffcc")
    a.delete_elements(1, [img.id, panel.id])
    assert a.doc[0].get_image_info() == []
    assert not any(d.get("fill") and hex(0) and fitz.Rect(d["rect"]) == panel.rect
                   for d in a.doc[0].get_drawings())
    assert "Contact: Alice" in page_text(a, 1)  # text in the panel stays
    a.delete_region(1, [70, 355, 525, 455])  # the table
    txt = page_text(a, 1)
    assert "North" not in txt and "Revenue" in txt  # paragraph above survives
    assert not [d for d in a.doc[0].get_drawings()
                if fitz.Rect(d["rect"]).intersects(fitz.Rect(80, 365, 500, 445))]


# ---- text editing -----------------------------------------------------------
def test_edit_text_keeps_original_typeface():
    # The fixture's heading is a subset CID font without a Unicode map, so the
    # installed copy of the same font is used (not a Base-14 lookalike).
    a = fresh()
    head = find_line(a, 1, "Quarterly")
    orig_font = head.info["style"]["font"]
    res = a.edit_text(1, head.id, "Quarterly Report")
    assert res["font"].startswith("QS") and "warning" not in res, res
    lay = a.layout(1)
    new = next(e for e in lay.of_kind("line") if "Quarterly" in e.lines[0].text)
    assert "LiberationSerif" in new.info["style"]["font"].replace(" ", ""), (orig_font, new.info)
    assert new.info["style"]["size"] == 22
    assert new.info["style"]["color"] == "#1a3380"
    assert abs(new.rect.y0 - head.rect.y0) < 3


def test_edit_text_reuses_full_embedded_font():
    path = os.path.join(TMP, "embedded.pdf")
    d = fitz.open()
    p = d.new_page()
    p.insert_font(fontname="DV", fontfile="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    p.insert_text((72, 100), "Embedded font line", fontname="DV", fontsize=14)
    d.save(path)
    a = AgentDocument(path)
    line = find_line(a, 1, "Embedded")
    res = a.edit_text(1, line.id, "Edited with the embedded font")
    assert res["font"].startswith("QF") and "warning" not in res, res
    assert res["font_size"] == 14 and "grew" in res["note"]
    assert find_line(a, 1, "Edited").info["style"]["font"] == line.info["style"]["font"]
    a.save(os.path.join(TMP, "embedded_out.pdf"))


def test_edit_text_growth_stops_at_neighbours():
    a = fresh()
    cell = find_line(a, 1, "North")  # table cell: column rule at x=296
    res = a.edit_text(1, cell.id, "Northern territories and islands")
    assert res["rect"][2] <= 296, res
    contact = find_line(a, 1, "Contact")  # inside the yellow panel (x1=300)
    res = a.edit_text(1, contact.id, "Contact: Alice Johnson-Smith, Regional Manager")
    assert res["rect"][2] <= 300 and res["rect"][3] <= 330, res


def test_edit_text_style_override_uses_base14():
    a = fresh()
    head = find_line(a, 1, "Quarterly")
    res = a.edit_text(1, head.id, "Annual Summary", bold=True)
    assert res["font"] == "tibo", res
    assert find_line(a, 1, "Annual Summary").info["style"]["bold"]


def test_edit_text_reflow_into_new_rect_and_shrink_warning():
    a = fresh()
    lay = a.layout(1)
    para = next(e for e in lay.of_kind("text") if e.lines[0].text.startswith("Revenue"))
    res = a.edit_text(1, para.id, "Short replacement paragraph.\nSecond line.",
                      rect=[72, 105, 300, 170])
    assert "warning" not in res
    txt = page_text(a, 1)
    assert "Short replacement paragraph." in txt and "Revenue grew" not in txt
    lay = a.layout(1)
    res = a.edit_text(1, find_line(a, 1, "DRAFT").id, "x " * 200)
    assert "warning" in res and res["font_size"] < 12


def test_replace_text_keeps_style_and_reports():
    a = fresh()
    res = a.replace_text("Alice", "Bob Smith")
    assert res["replacements"] == 1
    line = find_line(a, 1, "Bob Smith")
    assert line.lines[0].text.strip() == "Contact: Bob Smith"
    assert line.info["style"]["size"] == 12
    assert a.replace_text("no-such-text", "x")["replacements"] == 0
    res = a.replace_text("north", "Northern", match_case=False)
    assert res["replacements"] == 1 and "Northern" in page_text(a, 1)


# ---- adding content ---------------------------------------------------------
def test_add_text_html_shape_image_annotation():
    a = fresh()
    a.add_text(1, [320, 500, 520, 560], "Added note", font_size=14, color="#008000",
               bold=True)
    line = find_line(a, 1, "Added note")
    assert line.info["style"]["bold"] and line.info["style"]["color"] == "#008000"
    res = a.add_html(1, [72, 560, 300, 700], "<h3>Summary</h3><ul><li>One</li><li>Two</li></ul>")
    assert "Summary" in page_text(a, 1) and res["scale"] > 0
    a.add_shape(1, "rect", [400, 600, 500, 650], stroke_color="none", fill_color="#ff0000")
    assert pixel(a, 1, 450, 625)[:3] == (255, 0, 0)
    png = os.path.join(TMP, "dot.png")
    from PIL import Image
    Image.new("RGB", (10, 10), (0, 255, 0)).save(png)
    a.add_image(1, [400, 700, 450, 750], png)
    assert len(a.doc[0].get_image_info()) == 2
    a.add_annotation(1, "highlight", [72, 190, 220, 205], text="check this")
    page = a.doc[0]
    assert page.first_annot.type[1] == "Highlight"


# ---- re-layout --------------------------------------------------------------
def test_move_region_moves_panel_text_and_graphics():
    a = fresh()
    a.move_region(1, [70, 228, 302, 332], dy=400)
    lay = a.layout(1)
    contact = next(e for e in lay.of_kind("line") if "Contact" in e.lines[0].text)
    assert 640 < contact.rect.y0 < 650, contact.rect
    panel = [e for e in lay.of_kind("drawing") if e.info["fill"] == "#ffffcc"]
    assert len(panel) == 1 and abs(panel[0].rect.y0 - 630) < 2, panel
    # nothing left at the old location; the image (outside region) untouched
    assert a.doc[0].get_textbox(fitz.Rect(72, 230, 300, 330)).strip() == ""
    assert pixel(a, 1, 150, 300)[:3] == (255, 255, 255)
    assert fitz.Rect(a.doc[0].get_image_info()[0]["bbox"]) == fitz.Rect(350, 230, 500, 330)
    # moved content stays editable
    a.replace_text("Alice", "Carol")
    assert "Contact: Carol" in page_text(a, 1)


def test_move_elements_to_other_page_and_scale():
    a = fresh()
    lay = a.layout(1)
    img = lay.of_kind("image")[0]
    a.move_elements(1, [img.id], dx=-250, dy=300, to_page=2, scale=0.5)
    assert a.doc[0].get_image_info() == []
    info = a.doc[1].get_image_info()
    assert len(info) == 1
    r = fitz.Rect(info[0]["bbox"])
    assert abs(r.x0 - 100) < 1 and abs(r.y0 - 530) < 1 and abs(r.width - 75) < 1, r
    # the background of page 1 under the image did not travel with it
    assert pixel(a, 2, 101, 531)[:3] != (255, 255, 255)


def test_copy_keeps_original():
    a = fresh()
    draft = find_line(a, 1, "DRAFT")
    a.move_elements(1, [draft.id], dy=500, copy=True)
    assert page_text(a, 1).count("DRAFT") == 2


def test_move_region_carries_form_fields():
    a = fresh()
    a.move_region(2, [60, 100, 420, 250], dx=0, dy=300)
    fields = {f["name"]: f for f in a.list_fields(2)}
    assert abs(fields["name"]["bbox"][1] - 406) < 1, fields["name"]
    assert abs(fields["subscribe"]["bbox"][1] - 528) < 1
    assert "Name:" in a.doc[1].get_textbox(fitz.Rect(60, 400, 160, 430))


def test_rotated_page_coordinates_are_consistent():
    a = fresh()
    line = find_line(a, 3, "Landscape")
    a.move_elements(3, [line.id], dx=0, dy=200)
    moved = find_line(a, 3, "Landscape")
    assert abs(moved.rect.y0 - (line.rect.y0 + 200)) < 1, (line.rect, moved.rect)
    png, meta = render_page(a.doc[2], a.layout(3), grid=True)
    assert meta["rotation"] == 90 and meta["image_px"][0] > meta["image_px"][1]


# ---- forms ------------------------------------------------------------------
def test_forms_fill_add_update_delete_flatten():
    a = fresh()
    a.fill_fields({"name": "Jane Doe", "subscribe": True, "country": "France"})
    vals = {f["name"]: f["value"] for f in a.list_fields()}
    assert vals["name"] == "Jane Doe" and vals["country"] == "France"
    assert vals["subscribe"] not in (False, "Off", "")
    try:
        a.fill_fields({"country": "Narnia"})
    except EngineError:
        pass
    else:
        raise AssertionError("invalid option accepted")
    a.add_field(2, "text", "phone", [160, 266, 400, 284], value="555")
    a.update_field("phone", rect=[160, 300, 400, 318], label="Phone number",
                   required=True, new_name="phone_number")
    f = next(f for f in a.list_fields() if f["name"] == "phone_number")
    assert f["bbox"][1] == 300 and f.get("required") and f["value"] == "555"
    a.delete_field("email")
    assert "email" not in {f["name"] for f in a.list_fields()}
    a.flatten_fields()
    assert a.list_fields() == []
    assert "Jane Doe" in page_text(a, 2)


# ---- pages, undo, save ------------------------------------------------------
def test_page_ops_and_undo_redo():
    a = fresh()
    original = [page_text(a, p) for p in (1, 2, 3)]
    a.insert_blank_page(2)
    a.move_page(1, 4)
    a.rotate_pages([2], 90)
    a.duplicate_page(1)
    a.delete_pages([2])
    a.reorder_pages([3, 2, 1])
    assert a.doc.page_count == 3
    a.undo(6)
    assert [page_text(a, p) for p in (1, 2, 3)] == original
    a.redo(2)  # blank inserted at 2, then page 1 moved to the end
    assert a.doc.page_count == 4 and page_text(a, 1).strip() == ""
    assert "Registration" in page_text(a, 2) and "Quarterly" in page_text(a, 4)


def test_undo_restores_content_and_invalidates_ids():
    a = fresh()
    before = page_text(a, 1)
    a.replace_text("Alice", "Zed")
    a.delete_elements(1, [find_line(a, 1, "DRAFT").id])
    lay_ids = list(a.layout(1).elements)
    a.undo(2)
    assert page_text(a, 1) == before
    try:
        a.resolve(1, lay_ids[:1])
    except EngineError:
        pass
    else:
        raise AssertionError("ids survived undo")


def test_failed_edit_rolls_back():
    a = fresh()
    try:  # "name" is set before "country" fails validation inside the step
        a.fill_fields({"name": "Should not stick", "country": "Narnia"})
    except EngineError:
        pass
    else:
        raise AssertionError("invalid option accepted")
    vals = {f["name"]: f["value"] for f in a.list_fields()}
    assert vals["name"] == "" and a.history()["undo"] == [] and not a.dirty


def test_save_new_path_and_overwrite_with_backup():
    a = fresh("save_me.pdf")
    src = a.path
    a.replace_text("Alice", "Dana")
    out = os.path.join(TMP, "out", "edited.pdf")
    res = a.save(out)
    assert res["saved"] == out and not a.dirty
    assert "Contact: Dana" in fitz.open(out)[0].get_text()
    assert "Contact: Alice" in fitz.open(src)[0].get_text()
    b = AgentDocument(src)
    b.replace_text("Alice", "Eve")
    res = b.save()
    assert res["backup_of_original"].endswith("save_me.orig.pdf")
    assert "Eve" in fitz.open(src)[0].get_text()
    assert "Alice" in fitz.open(res["backup_of_original"])[0].get_text()


def test_font_lookup_distinguishes_base14_from_office_fonts():
    from pdfeditor.agent.engine import system_font_file
    assert system_font_file("Times-Roman") is None  # Base-14: exact without embedding
    assert system_font_file("Helvetica-BoldOblique") is None
    found = system_font_file("TimesNewRomanPS-BoldMT")
    if found:  # depends on installed fonts
        assert "bd" in found.lower() or "bold" in found.lower(), found


def test_ids_follow_reading_order_after_edits():
    a = fresh()
    ids = {e.id: e.lines[0].text for e in a.layout(1).of_kind("text")}
    a.replace_text("Alice", "Alicia")
    after = {e.id: e.lines[0].text for e in a.layout(1).of_kind("text")}
    assert [k for k, v in ids.items() if "Contact" in v] == \
        [k for k, v in after.items() if "Contact" in v]


def test_backup_only_when_overwriting_original():
    a = fresh("bk.pdf")
    out = os.path.join(TMP, "bk-out.pdf")
    a.save(out)
    res = a.save(os.path.join(TMP, "bk-out.pdf"))  # now the doc's own path
    assert res.get("backup_of_original", "").endswith("bk-out.orig.pdf")
    b = fresh("bk2.pdf")
    open(os.path.join(TMP, "existing.pdf"), "wb").write(b"%PDF-1.4 placeholder")
    res = b.save(os.path.join(TMP, "existing.pdf"))
    assert "backup_of_original" not in res


def test_pending_redactions_are_not_applied_silently():
    a = fresh()
    pg = a.doc[0]
    pg.add_redact_annot(fitz.Rect(72, 105, 300, 120))
    try:
        a.delete_elements(1, [find_line(a, 1, "DRAFT").id])
    except EngineError as exc:
        assert "pending redaction" in str(exc)
    else:
        raise AssertionError("pre-existing redaction was applied")
    assert "Revenue grew" in page_text(a, 1)
    a.delete_elements(1, [e.id for e in a.layout(1).of_kind("annotation")])
    a.delete_elements(1, [find_line(a, 1, "DRAFT").id])
    assert "Revenue grew" in page_text(a, 1) and "DRAFT" not in page_text(a, 1)


def test_render_overlay_and_region():
    a = fresh()
    png, meta = render_page(a.doc[0], a.layout(1))
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and max(meta["image_px"]) == 1400
    assert meta["labelled_elements"] >= 10
    png, meta = render_page(a.doc[0], None, region=fitz.Rect(72, 230, 300, 330))
    assert meta["image_px"][0] > meta["image_px"][1] and meta["zoom"] > 4


def _run_all() -> int:
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed  (artifacts in {TMP})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
