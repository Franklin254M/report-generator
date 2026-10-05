"""Turn cleaning statistics into the issue list and the quality score shown to the client."""


def quality_score(clean_rows: int, input_rows: int) -> float:
    """Share of INPUT rows that end up with a valid date, a valid amount, and no flag.
    Removed duplicates count against the score because they were bad input rows."""
    return round(100 * clean_rows / input_rows, 1) if input_rows else 0.0


def build_issues(exact_removed: int, id_removed: int, stats: dict) -> list[dict]:
    d, a = stats["dates"], stats["amounts"]
    candidates = [
        ("duplicates", exact_removed, "Exact duplicate rows removed"),
        ("duplicate_ids", id_removed, "Rows sharing an ID with different content removed (first kept, see review file)"),
        ("ambiguous_dates", d["ambiguous"], "Slash dates that could be read two ways (parsed with the chosen order)"),
        ("missing_dates", d["missing"], "Rows with no date"),
        ("unreadable_dates", d["unreadable"], "Dates that couldn't be read (left blank)"),
        ("missing_amounts", a["missing"], "Rows with no amount"),
        ("unreadable_amounts", a["unreadable"], "Amounts that couldn't be read (left blank)"),
        ("outliers", stats["outliers"], "Unusual amounts flagged, not removed"),
    ]
    return [{"type": t, "count": n, "note": note} for t, n, note in candidates if n]
