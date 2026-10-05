"""Decide which columns hold the date and amount. Ask the user when we can't tell."""
import pandas as pd


def find_columns(df: pd.DataFrame, rules: dict, date_column: str | None = None,
                 amount_column: str | None = None) -> tuple[dict, dict | None]:
    """Return (mapping, needs_input). needs_input is None when both columns were found."""
    overrides = {"date": date_column, "amount": amount_column}
    lowered = {c.lower(): c for c in df.columns}
    mapping: dict[str, str] = {}
    for field, candidates in rules["required_columns"].items():
        explicit = overrides.get(field)
        if explicit:
            if explicit not in df.columns:
                return mapping, {"field": f"{field}_column", "options": list(df.columns)}
            mapping[field] = explicit
            continue
        found = next((lowered[c] for c in candidates if c in lowered), None)
        if found is None:
            return mapping, {"field": f"{field}_column", "options": list(df.columns)}
        mapping[field] = found
    return mapping, None
