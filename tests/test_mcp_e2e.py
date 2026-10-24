"""End-to-end test: launch the MCP server over stdio (as Claude would) and drive
a full open -> inspect -> edit -> verify -> save session through it.

Run:  .venv/bin/python tests/test_mcp_e2e.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

import fitz
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import build_fixture  # noqa: E402


def _json(result):
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


async def main() -> None:
    tmp = tempfile.mkdtemp(prefix="quill-mcp-")
    src = build_fixture(os.path.join(tmp, "report.pdf"))
    out = os.path.join(tmp, "report-edited.pdf")
    params = StdioServerParameters(command=os.path.join(ROOT, "mcp-server.sh"), args=[])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            init = await s.initialize()
            assert "get_page_layout" in (init.instructions or "")
            names = {t.name for t in (await s.list_tools()).tools}
            for required in ("open_pdf", "render_page", "get_page_layout", "edit_text",
                             "move_region", "fill_form", "save_pdf", "undo"):
                assert required in names, required
            print(f"tools: {len(names)}")

            info = _json(await s.call_tool("open_pdf", {"path": src}))
            assert info["pages"] == 3 and info["doc"] == "report"

            shot = await s.call_tool("render_page", {"page": 1, "overlay": True})
            assert [c.type for c in shot.content] == ["text", "image"]
            assert shot.content[1].mime_type == "image/png"

            lay = _json(await s.call_tool("get_page_layout", {"page": 1}))
            draft = next(t for t in lay["text"] if t["text"].startswith("DRAFT"))
            res = _json(await s.call_tool("delete_elements", {"page": 1, "ids": [draft["id"]]}))
            assert res["changed_pages"] == [1]

            # stale id -> a clear tool error, not a wrong edit
            bad = await s.call_tool("delete_elements", {"page": 1, "ids": ["t0"]})
            assert bad.is_error and "out of date" in bad.content[0].text

            hits = _json(await s.call_tool("find_text", {"query": "Quarterly"}))
            res = _json(await s.call_tool("edit_text", {
                "page": 1, "id": hits["hits"][0]["line_id"], "new_text": "Annual Report"}))
            assert res["font_size"] == 22

            _json(await s.call_tool("move_region", {"page": 1, "rect": [70, 228, 302, 332],
                                                    "dy": 380}))
            _json(await s.call_tool("fill_form", {"values": {"name": "Jane", "subscribe": True}}))
            fields = _json(await s.call_tool("list_form_fields", {}))
            assert {f["name"]: f["value"] for f in fields["fields"]}["name"] == "Jane"

            _json(await s.call_tool("add_text", {"page": 1, "rect": [72, 500, 400, 530],
                                                 "text": "Approved", "bold": True}))
            _json(await s.call_tool("undo", {}))
            _json(await s.call_tool("redo", {}))

            bad = await s.call_tool("open_pdf", {"path": "/no/such.pdf"})
            assert bad.is_error and "No such file" in bad.content[0].text

            saved = _json(await s.call_tool("save_pdf", {"output_path": out}))
            assert saved["saved"] == out

    page = fitz.open(out)[0]
    text = page.get_text()
    assert "Annual Report" in text and "DRAFT" not in text and "Approved" in text
    assert fitz.open(out)[1].first_widget.field_value == "Jane"
    assert "Quarterly" in fitz.open(src)[0].get_text()  # original untouched
    print(f"PASS end-to-end MCP session  ({out})")


if __name__ == "__main__":
    asyncio.run(main())
