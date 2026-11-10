"""GO-aware parsing helpers."""

import re

import sqlglot

GO_PATTERN = r"(?im)^\s*GO\s*$"


def split_batches(sql: str) -> list[tuple[int, int, str]]:
    """Split at standalone GO lines, keeping exact offsets into the original script.

    Returns (start, end, text) triples with the invariant sql[start:end] == text. The GO
    separator itself (and the whitespace the pattern swallows around it) belongs to no batch,
    so offsets are never shortened by the separators the split discards.
    """
    parts, cursor = [], 0
    for match in re.finditer(GO_PATTERN, sql):
        parts.append((cursor, match.start(), sql[cursor:match.start()]))
        cursor = match.end()
    parts.append((cursor, len(sql), sql[cursor:]))
    return parts


def parse_batches(sql: str) -> list[dict]:
    """Split at standalone GO lines and parse each non-empty batch independently."""
    batches = []
    for index, (start, end, text) in enumerate(split_batches(sql)):
        item = {"index": index, "start": start, "end": end, "sql": text,
                "parsed": False, "statement_count": 0, "diagnostics": [], "expressions": []}
        if not text.strip():
            item["parsed"] = True
        else:
            try:
                item["expressions"] = sqlglot.parse(text, dialect="tsql")
                item["parsed"] = True
                item["statement_count"] = len(item["expressions"])
            except Exception as exc:
                item["diagnostics"].append({"level": "error", "reason": str(exc)})
        batches.append(item)
    return batches
