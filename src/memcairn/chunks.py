"""Split a markdown document into retrievable chunks, so recall can return one part.

Rules (from the document-ingestion research, see DESIGN.md):
- split at H1–H3; deeper headings stay in their parent section;
- every chunk carries its heading path ("doc › H1 › H2"), indexed and shown;
- a table with more than TABLE_ROWS_INLINE rows becomes one chunk per row, each with the
  header repeated, anchored by the row's first cell (a server name stays its anchor even
  when rows are reordered);
- fenced code blocks are never split;
- sections over MAX_CHARS split at blank lines;
- front matter is skipped.
Anchors are stable slugs of the heading path (+ row key), de-duplicated with -2, -3.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

MAX_CHARS = 2400  # ~700 tokens: hard cap per chunk
TABLE_ROWS_INLINE = 2  # inventories: one row per server
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FRONTMATTER = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.S)


@dataclass
class Chunk:
    anchor: str
    path: str  # "H1 › H2"
    kind: str  # section | row
    content: str

    @property
    def sha(self) -> str:
        return hashlib.sha1(f"{self.path}\n{self.content}".encode()).hexdigest()


def slug(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text.lower())
    ascii_ = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", ascii_).strip("-")[:60] or "x"


def _is_table_line(line: str) -> bool:
    return line.lstrip().startswith("|")


def _cells(line: str) -> list[str]:
    parts = re.split(r"(?<!\\)\|", line.strip().strip("|"))
    return [p.strip().replace("\\|", "|") for p in parts]


def _split_long(text: str) -> list[str]:
    """Split at blank lines (never inside a code fence) into parts under MAX_CHARS."""
    if len(text) <= MAX_CHARS:
        return [text]
    blocks, cur, fence = [], [], False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            fence = not fence
        cur.append(line)
        if not fence and not line.strip():
            blocks.append("\n".join(cur))
            cur = []
    if cur:
        blocks.append("\n".join(cur))
    # a block still over the cap (a long list, no blank lines) splits between lines,
    # unless it is a code fence, which stays whole
    fine = []
    for b in blocks:
        if len(b) <= MAX_CHARS or b.lstrip().startswith("```"):
            fine.append(b)
            continue
        piece = ""
        for line in b.splitlines():
            if piece and len(piece) + len(line) > MAX_CHARS:
                fine.append(piece)
                piece = ""
            piece += line + "\n"
        if piece:
            fine.append(piece)
    blocks = fine
    parts, buf = [], ""
    for b in blocks:
        if buf and len(buf) + len(b) > MAX_CHARS:
            parts.append(buf.strip())
            buf = ""
        buf += b + "\n"
    if buf.strip():
        parts.append(buf.strip())
    return parts


def chunk_markdown(text: str) -> list[Chunk]:
    text = FRONTMATTER.sub("", text, count=1)
    sections: list[tuple[list[str], list[str]]] = [([], [])]  # (heading path, lines)
    stack: list[tuple[int, str]] = []  # (level, title); works when a document skips H1
    fence = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            fence = not fence
        m = None if fence else HEADING.match(line)
        if m and len(m.group(1)) <= 3:
            level = len(m.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, m.group(2).strip()))
            sections.append(([t for _, t in stack], []))
        else:
            sections[-1][1].append(line)

    chunks: list[Chunk] = []
    for heads, lines in sections:
        p = " › ".join(heads)
        body, tables, i = [], [], 0
        while i < len(lines):  # pull out big tables
            if _is_table_line(lines[i]):
                j = i
                while j < len(lines) and _is_table_line(lines[j]):
                    j += 1
                block = lines[i:j]
                rows = [r for r in block[2:] if r.strip()] if len(block) > 2 else []
                if len(rows) > TABLE_ROWS_INLINE:
                    tables.append((block[0], rows))
                    body.append(f"(table with {len(rows)} rows, one chunk per row)")
                else:
                    body += block
                i = j
            else:
                body.append(lines[i])
                i += 1
        content = "\n".join(body).strip()
        base = slug(heads[-1]) if heads else "intro"
        if content:
            for n, part in enumerate(_split_long(content)):
                chunks.append(Chunk(base if n == 0 else f"{base}-{n + 1}", p, "section", part))
        for header, rows in tables:
            for row in rows:
                cells = _cells(row)
                key = next((c for c in cells if c and c != "—"), "row")
                chunks.append(Chunk(slug(key), p, "row", f"{header}\n{row}"))
    seen: dict[str, int] = {}
    for c in chunks:  # de-duplicate anchors (two "Notes" sections, repeated row keys)
        n = seen.get(c.anchor, 0) + 1
        seen[c.anchor] = n
        if n > 1:
            c.anchor = f"{c.anchor}-{n}"
    return chunks


def outline(text: str) -> str:
    """Headings only, for a cheap table of contents."""
    text = FRONTMATTER.sub("", text, count=1)
    out, fence = [], False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            fence = not fence
        m = None if fence else HEADING.match(line)
        if m:
            out.append("  " * (len(m.group(1)) - 1) + m.group(2).strip())
    return "\n".join(out)


def row_pairs(content: str) -> str:
    """'header\nrow' of a row chunk as 'col: value · col: value' (empty cells left out)."""
    lines = content.splitlines()
    if len(lines) < 2:
        return content
    head, row = _cells(lines[0]), _cells(lines[-1])
    return " · ".join(f"{h}: {v}" if h else v for h, v in zip(head, row, strict=False)
                      if v and v != "—")
