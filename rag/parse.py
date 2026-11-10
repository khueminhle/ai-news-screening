"""Turn SEC / Fed documents (HTML, plain text, PDF) into clean text blocks, split by section.

Output of parse_document():
    {
      "sections_found": True / False / None,   # None = this kind of document is not split
      "warnings": [...],
      "blocks": [ {"section": "Item 1A", "heading": "Item 1A. Risk Factors",
                   "kind": "text" | "table", "content": "..."}, ... ]
    }

Rules:
  - Hidden XBRL data, scripts and styles are removed.
  - Tables keep their rows: one line per row, cells joined with " | ".
    "$", ")" and "%" that sit in their own cells are glued back to the number.
  - Tables used only for page layout (one column, or a lone heading) become text.
  - 10-K and 10-Q main documents are split by "Item" sections (10-Q also by Part),
    8-K main documents by "Item 2.02"-style items. Everything else is one section.
  - If the expected sections can't be found reliably, the document is kept whole
    with sections_found = False and a warning.
"""
from __future__ import annotations

import re
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString

BLOCK_TAGS = ["p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
              "center", "blockquote", "pre", "dd", "dt"]
TABLE_MARK = "\x00TABLE{}\x00"
ITEM_RE = re.compile(r"^\s*item\s*(\d\.\d{2}|\d{1,2}[a-c]?)\b\s*[\.\:\-—–]?\s*(.*)$", re.I)  # 8-K "2.02" first
PART_RE = re.compile(r"^\s*part\s+(iv|i{1,3})\b", re.I)
HEADING_MAX_CHARS = 200
TEXT_BLOCK_MAX_CHARS = 3000

# How many distinct sections must be found before we trust the split.
MIN_SECTIONS = {"10-K": 5, "10-Q": 3, "8-K": 1}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _merge_symbol_cells(cells: list[str]) -> list[str]:
    """['$', '1,026,248', '(3,182', ')'] -> ['$1,026,248', '(3,182)']"""
    out: list[str] = []
    pending_prefix = ""
    for c in cells:
        if c in ("$", "€", "£", "¥"):
            pending_prefix += c
            continue
        if c in (")", "%", ")%", "%)") and out:
            out[-1] += c
            continue
        out.append(pending_prefix + c)
        pending_prefix = ""
    return out


def _table_rows(table) -> list[list[str]]:
    rows = []
    for tr in table.find_all("tr"):
        if tr.find_parent("table") is not table:
            continue
        cells = [_norm(td.get_text(" ")) for td in tr.find_all(["td", "th"]) if td.find_parent("tr") is tr]
        cells = _merge_symbol_cells([c for c in cells if c])
        if cells:
            rows.append(cells)
    return rows


def html_to_lines(html: bytes | str, root_selector: str | None = None) -> list[dict]:
    """HTML -> ordered list of {"kind": "text"|"table", "content": str}, one text line per paragraph."""
    soup = BeautifulSoup(html, "html.parser")
    root = (soup.select_one(root_selector) if root_selector else None) or soup.body or soup
    for t in root.find_all(["script", "style", "head", "ix:header"]):
        t.decompose()
    for t in root.find_all(style=re.compile(r"display\s*:\s*none", re.I)):
        t.decompose()

    tables: list[dict] = []
    for table in root.find_all("table"):
        if table.find_parent("table") is not None:
            continue  # nested tables are read as part of their outer table
        rows = _table_rows(table)
        joined = " ".join(" ".join(r) for r in rows)
        ncols = max((len(r) for r in rows), default=0)
        if not rows:
            replacement = ""
        elif ncols <= 1 or (len(rows) == 1 and (ITEM_RE.match(joined) or PART_RE.match(joined))):
            replacement = "\n" + "\n".join(" ".join(r) for r in rows) + "\n"  # layout table -> text
        else:
            tables.append({"kind": "table", "content": "\n".join(" | ".join(r) for r in rows)})
            replacement = "\n" + TABLE_MARK.format(len(tables) - 1) + "\n"
        table.replace_with(NavigableString(replacement))

    for br in root.find_all("br"):
        br.replace_with(NavigableString("\n"))
    for tag in root.find_all(BLOCK_TAGS):
        tag.insert_after(NavigableString("\n"))

    lines: list[dict] = []
    for raw in root.get_text("").split("\n"):
        line = _norm(raw)
        if not line:
            continue
        m = re.fullmatch(r"\x00TABLE(\d+)\x00", line)
        lines.append(tables[int(m.group(1))] if m else {"kind": "text", "content": line})
    return lines


def text_to_lines(text: str) -> list[dict]:
    paragraphs = re.split(r"\n\s*\n", text.replace("\r\n", "\n"))
    return [{"kind": "text", "content": _norm(p)} for p in paragraphs if _norm(p)]


def pdf_to_lines(path: Path) -> list[dict]:
    import pymupdf  # imported here so the rest works even if PyMuPDF is missing

    lines = []
    with pymupdf.open(path) as pdf:
        for page_no, page in enumerate(pdf, start=1):
            for para in re.split(r"\n\s*\n", page.get_text()):
                if _norm(para):
                    lines.append({"kind": "text", "content": _norm(para), "page": page_no})
    return lines


# ---------- sections ----------

def _form_family(doc_type: str) -> str | None:
    t = (doc_type or "").upper()
    for fam in ("10-K", "10-Q", "8-K"):
        if t.startswith(fam):
            return fam
    return None


def split_sections(lines: list[dict], doc_type: str) -> tuple[list[dict], bool | None, list[str]]:
    """Label every line with its section. Returns (labelled lines, sections_found, warnings)."""
    family = _form_family(doc_type)
    if family is None:
        return [{**ln, "section": doc_type or "Document", "heading": None} for ln in lines], None, []

    candidates = []  # (line index, label, heading text)
    part = None
    for i, ln in enumerate(lines):
        if ln["kind"] != "text" or len(ln["content"]) > HEADING_MAX_CHARS:
            continue
        pm = PART_RE.match(ln["content"])
        if pm:
            part = pm.group(1).upper()
        m = ITEM_RE.match(ln["content"])
        if not m:
            continue
        code = m.group(1).upper()
        if (family == "8-K") != ("." in code):
            continue  # 8-K items look like 2.02; 10-K/10-Q items like 1A
        label = f"Part {part} Item {code}" if family == "10-Q" and part else f"Item {code}"
        candidates.append((i, label, ln["content"]))

    # The table of contents lists every item too. For each label keep the occurrence
    # followed by the most text: that is the real section, not the contents line.
    def size(k: int) -> int:
        start = candidates[k][0]
        end = candidates[k + 1][0] if k + 1 < len(candidates) else len(lines)
        return sum(len(ln["content"]) for ln in lines[start:end])

    best: dict[str, tuple[int, int]] = {}
    for k, (idx, label, _) in enumerate(candidates):
        s = size(k)
        if label not in best or s > best[label][1]:
            best[label] = (idx, s)
    kept = sorted((idx, label) for label, (idx, _) in best.items())
    headings = {idx: heading for idx, _, heading in candidates}

    warnings: list[str] = []
    if len(kept) < MIN_SECTIONS[family]:
        warnings.append(f"only {len(kept)} {family} section heading(s) found; kept as one unsplit text")
        return [{**ln, "section": "Full text", "heading": None} for ln in lines], False, warnings

    labelled = []
    current, heading = "Cover", None
    starts = {idx: label for idx, label in kept}
    for i, ln in enumerate(lines):
        if i in starts:
            current, heading = starts[i], headings[i]
        labelled.append({**ln, "section": current, "heading": heading})
    return labelled, True, warnings


def merge_text(labelled: list[dict]) -> list[dict]:
    """Join consecutive text lines of the same section into blocks of up to ~3,000 characters."""
    blocks: list[dict] = []
    for ln in labelled:
        prev = blocks[-1] if blocks else None
        if (ln["kind"] == "text" and prev and prev["kind"] == "text" and prev["section"] == ln["section"]
                and len(prev["content"]) + len(ln["content"]) < TEXT_BLOCK_MAX_CHARS):
            prev["content"] += "\n" + ln["content"]
        else:
            blocks.append({"section": ln["section"], "heading": ln["heading"], "kind": ln["kind"],
                           "content": ln["content"]})
    return blocks


def parse_document(path: Path, doc_type: str, root_selector: str | None = None,
                   drop_lines: tuple[str, ...] = ()) -> dict:
    path = Path(path)
    suffix = path.suffix.lower()
    warnings: list[str] = []
    if suffix in (".htm", ".html"):
        lines = html_to_lines(path.read_bytes(), root_selector)
    elif suffix == ".txt":
        lines = text_to_lines(path.read_text(encoding="utf-8", errors="replace"))
    elif suffix == ".pdf":
        try:
            lines = pdf_to_lines(path)
        except Exception as exc:  # PyMuPDF missing or a damaged PDF
            return {"sections_found": None, "warnings": [f"PDF not readable: {exc}"], "blocks": []}
    else:
        return {"sections_found": None, "warnings": [f"unsupported file type {suffix}"], "blocks": []}

    lines = [ln for ln in lines if ln["content"] not in drop_lines]
    if not lines:
        warnings.append("no text found")
    labelled, found, split_warnings = split_sections(lines, doc_type)
    return {"sections_found": found, "warnings": warnings + split_warnings, "blocks": merge_text(labelled)}
