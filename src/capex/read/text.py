"""Extract readable plain text from SEC HTML or HKEX PDF filings.

The goal is "good enough for an LLM to read" — not pixel-perfect
rendering. We preserve headings, table structure (as tab-separated
rows), and paragraph boundaries. We strip all styling, scripts, XBRL
inline tags, and other noise.

Stdlib only for HTML (regex + html.parser). PDF extraction requires
`pypdf` (declared in pyproject.toml under the `[read]` extra).

Functions:
    extract_text(path) → str
        Auto-detects format from extension and dispatches.
    extract_text_from_html(path) → str
        SEC Inline XBRL HTML → plain text with headings and tables.
    extract_text_from_pdf(path) → str
        HKEX PDFs → plain text via pypdf.
"""
from __future__ import annotations

import html
import re
from pathlib import Path


def extract_text(path: Path) -> str:
    """Auto-detect format and extract readable text."""
    suffix = path.suffix.lower()
    if suffix in (".htm", ".html", ".xhtml"):
        return extract_text_from_html(path)
    elif suffix == ".pdf":
        return extract_text_from_pdf(path)
    else:
        raise ValueError(f"unsupported file extension for text extraction: {suffix}")


def extract_text_from_html(path: Path) -> str:
    """Extract readable text from SEC Inline XBRL HTML.

    Strategy:
    1. Remove <script>, <style>, and <head> blocks entirely.
    2. Convert <table> blocks to tab-separated text tables.
    3. Insert newlines at block-level element boundaries.
    4. Strip all remaining HTML tags.
    5. Decode HTML entities.
    6. Collapse excessive whitespace while preserving paragraph structure.
    """
    raw = path.read_text(encoding="utf-8", errors="replace")

    # 1. Remove script, style, head blocks
    text = re.sub(r"<script[^>]*>.*?</script>", "", raw, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<head[^>]*>.*?</head>", "", text, flags=re.DOTALL | re.IGNORECASE)

    # 2. Convert tables to text — preserve row/column structure
    text = _convert_tables(text)

    # 3. Insert newlines at block-level boundaries
    block_tags = r"(?:div|p|h[1-6]|tr|li|br|hr|section|article|header|footer|blockquote)"
    text = re.sub(rf"</?{block_tags}[^>]*>", "\n", text, flags=re.IGNORECASE)

    # 4. Strip all remaining tags
    text = re.sub(r"<[^>]+>", "", text)

    # 5. Decode HTML entities
    text = html.unescape(text)

    # 6. Collapse whitespace
    # Replace runs of spaces/tabs (not newlines) with a single space
    text = re.sub(r"[^\S\n]+", " ", text)
    # Collapse 3+ consecutive newlines to 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Strip leading/trailing whitespace per line
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)
    # Remove blank lines that are just whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def extract_text_from_pdf(path: Path) -> str:
    """Extract text from a PDF, preserving table structure.

    Uses pdfplumber (preferred) for better table extraction, or falls
    back to pypdf. pdfplumber detects table boundaries and preserves
    column alignment — critical for financial statements in HKEX PDFs.

    Both handle Chinese/CJK text natively.
    """
    try:
        import pdfplumber

        pages = []
        with pdfplumber.open(str(path)) as pdf:
            for i, page in enumerate(pdf.pages, 1):
                # Extract tables separately for better structure
                tables = page.extract_tables()
                page_text = page.extract_text() or ""

                if tables:
                    # Replace inline text with structured table output
                    table_texts = []
                    for table in tables:
                        rows = []
                        for row in table:
                            cells = [
                                str(c).strip() if c else ""
                                for c in row
                            ]
                            if any(cells):
                                rows.append("\t".join(cells))
                        if rows:
                            table_texts.append(
                                "\n[TABLE]\n"
                                + "\n".join(rows)
                                + "\n[/TABLE]"
                            )
                    if table_texts:
                        page_text += "\n" + "\n".join(table_texts)

                if page_text.strip():
                    pages.append(f"[Page {i}]\n{page_text}")
        return "\n\n".join(pages)

    except ImportError:
        pass

    # Fallback to pypdf
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ImportError(
            "pdfplumber or pypdf is required for PDF text extraction. "
            "Install with: pip install pdfplumber"
        ) from exc

    reader = PdfReader(str(path))
    pages = []
    for i, page in enumerate(reader.pages, 1):
        page_text = page.extract_text() or ""
        if page_text.strip():
            pages.append(f"[Page {i}]\n{page_text}")
    return "\n\n".join(pages)


# ----------------------------------------------------------------------------
# Table conversion
# ----------------------------------------------------------------------------


def _convert_tables(html_text: str) -> str:
    """Replace <table>...</table> blocks with tab-separated text tables.

    Each <tr> becomes a line. Each <td>/<th> becomes a tab-separated cell.
    The table is wrapped in markers so the LLM can see table boundaries.
    """

    def _table_to_text(match: re.Match) -> str:
        table_html = match.group(0)
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.DOTALL | re.IGNORECASE)
        text_rows = []
        for row_html in rows:
            cells = re.findall(
                r"<(?:td|th)[^>]*>(.*?)</(?:td|th)>", row_html, re.DOTALL | re.IGNORECASE
            )
            cleaned = []
            for cell in cells:
                # Strip tags inside cells, collapse whitespace
                cell_text = re.sub(r"<[^>]+>", "", cell)
                cell_text = html.unescape(cell_text)
                cell_text = " ".join(cell_text.split())
                cleaned.append(cell_text)
            if any(c.strip() for c in cleaned):
                text_rows.append("\t".join(cleaned))
        if not text_rows:
            return ""
        return "\n[TABLE]\n" + "\n".join(text_rows) + "\n[/TABLE]\n"

    return re.sub(
        r"<table[^>]*>.*?</table>", _table_to_text, html_text, flags=re.DOTALL | re.IGNORECASE
    )
