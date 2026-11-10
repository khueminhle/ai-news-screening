"""Document extraction: tables kept as rows, sections split, unclear documents kept whole."""
from pathlib import Path

from rag.parse import html_to_lines, parse_document, split_sections

FIXTURES = Path(__file__).parent / "fixtures"

TABLE_HTML = """<html><body>
<p>Results</p>
<table>
  <tr><td>Three Months Ended</td><td>2026</td><td></td><td>2025</td></tr>
  <tr><td>Revenues</td><td>$</td><td>1,026,248</td><td>$</td><td>733,773</td></tr>
  <tr><td>Interest and other</td><td>6,162</td><td>(3,182</td><td>)</td></tr>
</table>
<div style="display:none">hidden xbrl data</div>
</body></html>"""

TEN_K_HTML = """<html><body>
<p>Table of Contents</p>
<table><tr><td>Item 1.</td><td>Business</td><td>3</td></tr>
       <tr><td>Item 1A.</td><td>Risk Factors</td><td>9</td></tr></table>
<p>Item 1. Business</p><p>Short contents line.</p>
<p>Item 1. Business</p><p>{body}</p>
<p>Item 1A. Risk Factors</p><p>{body}</p>
<p>Item 2. Properties</p><p>{body}</p>
<p>Item 7. Management's Discussion and Analysis</p><p>{body}</p>
<p>Item 8. Financial Statements</p><p>{body}</p>
</body></html>""".replace("{body}", "Long section text. " * 50)


def test_table_rows_and_symbol_cells():
    lines = html_to_lines(TABLE_HTML)
    table = next(ln for ln in lines if ln["kind"] == "table")
    assert "Revenues | $1,026,248 | $733,773" in table["content"]
    assert "Interest and other | 6,162 | (3,182)" in table["content"]
    assert not any("hidden xbrl" in ln["content"] for ln in lines)


def test_10k_sections_skip_table_of_contents(tmp_path):
    p = tmp_path / "k.htm"
    p.write_text(TEN_K_HTML, encoding="utf-8")
    parsed = parse_document(p, "10-K")
    assert parsed["sections_found"] is True
    sections = list(dict.fromkeys(b["section"] for b in parsed["blocks"]))
    assert sections == ["Cover", "Item 1", "Item 1A", "Item 2", "Item 7", "Item 8"]
    item1 = "".join(b["content"] for b in parsed["blocks"] if b["section"] == "Item 1")
    assert "Long section text" in item1  # the real section, not the contents line


def test_real_8k_split_by_item():
    parsed = parse_document(FIXTURES / "sec_8k_main.htm", "8-K")
    assert parsed["sections_found"] is True
    sections = {b["section"] for b in parsed["blocks"]}
    assert {"Item 2.02", "Item 9.01"} <= sections


def test_unclear_document_kept_whole_with_warning():
    lines = [{"kind": "text", "content": "No item headings here at all."}]
    labelled, found, warnings = split_sections(lines, "10-K")
    assert found is False and warnings
    assert labelled[0]["section"] == "Full text"


def test_exhibits_are_not_split():
    lines = [{"kind": "text", "content": "Press release text"}]
    labelled, found, _ = split_sections(lines, "EX-99.1")
    assert found is None and labelled[0]["section"] == "EX-99.1"
