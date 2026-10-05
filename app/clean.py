"""Cleaning rules. Nothing here invents data: unreadable values become blank and are counted."""
import re

import pandas as pd

SLASH_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})$")


def blank_to_na(series: pd.Series, null_tokens: list[str]) -> pd.Series:
    s = series.astype("string").str.strip()
    return s.mask(s.isna() | s.str.lower().isin(null_tokens), pd.NA)


# ---------------------------------------------------------------- duplicates
def remove_duplicates(df: pd.DataFrame) -> tuple[pd.DataFrame, int, pd.DataFrame, int]:
    """Drop exact duplicate rows. For rows sharing a transaction_id but differing in content,
    keep the first and put every row of the group in a review table (nothing is lost silently).

    Returns (kept, exact_removed, review, id_removed).
    """
    original_cols = [c for c in df.columns if c != "data_row"]
    exact = df.duplicated(subset=original_cols, keep="first")
    exact_removed = int(exact.sum())
    df = df[~exact]

    review = pd.DataFrame(columns=["resolution", *df.columns])
    id_removed = 0
    id_col = next((c for c in df.columns if c.lower() == "transaction_id"), None)
    if id_col is not None:
        ids = df[id_col].astype("string").str.strip()
        in_group = ids.notna() & ids.duplicated(keep=False)
        if in_group.any():
            is_repeat = ids.notna() & ids.duplicated(keep="first")
            group = df[in_group].copy()
            group.insert(0, "resolution", ["removed" if r else "kept" for r in is_repeat[in_group]])
            review = group.sort_values(["data_row"]).reset_index(drop=True)
            id_removed = int(is_repeat.sum())
            df = df[~is_repeat]
    return df, exact_removed, review, id_removed


# --------------------------------------------------------------------- dates
def detect_date_order(raw: pd.Series, rules: dict) -> tuple[str, str]:
    """Read the evidence in the file: 03/25/23 can only be month-first, 25/03/23 only day-first.
    Returns (order, source) where source is detected, conflict (both seen), or assumed (no evidence)."""
    default = rules["date"]["default_order"]
    month_evidence = day_evidence = 0
    for value in blank_to_na(raw, rules["null_tokens"]).dropna():
        m = SLASH_DATE.match(value)
        if not m:
            continue
        first, second = int(m[1]), int(m[2])
        if second > 12 >= first:
            month_evidence += 1
        elif first > 12 >= second:
            day_evidence += 1
    if month_evidence and day_evidence:
        return default, "conflict"
    if month_evidence:
        return "month", "detected"
    if day_evidence:
        return "day", "detected"
    return default, "assumed"


def parse_dates(raw: pd.Series, rules: dict, order: str) -> tuple[pd.Series, dict]:
    s = blank_to_na(raw, rules["null_tokens"])
    cfg = rules["date"]
    formats = [*cfg["formats_any_order"], *cfg["slash_formats"][order]]

    parsed = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    for fmt in formats:
        todo = parsed.isna() & s.notna()
        if not todo.any():
            break
        attempt = pd.to_datetime(s[todo], format=fmt, errors="coerce").astype("datetime64[ns]")
        parsed.loc[todo] = attempt

    ambiguous = 0
    for value in s.dropna():
        m = SLASH_DATE.match(value)
        if m and int(m[1]) <= 12 and int(m[2]) <= 12 and m[1] != m[2]:
            ambiguous += 1

    stats = {
        "missing": int(s.isna().sum()),
        "unreadable": int((s.notna() & parsed.isna()).sum()),
        "ambiguous": ambiguous,
    }
    return parsed, stats


# ------------------------------------------------------------------- amounts
def parse_amounts(raw: pd.Series, rules: dict) -> tuple[pd.Series, dict]:
    cfg = rules["amount"]
    s = blank_to_na(raw, rules["null_tokens"])
    negative = pd.Series(False, index=s.index)
    if cfg.get("parentheses_are_negative", False):
        negative = s.str.match(r"^\(.*\)$").fillna(False).astype(bool)
        s = s.str.replace(r"^\((.*)\)$", r"\1", regex=True)
    tokens = [re.escape(t) for t in [*cfg["currency_symbols"], *cfg["currency_words"]]]
    if tokens:
        s = s.str.replace("|".join(tokens), "", regex=True, case=False)
    s = s.str.replace(r"[,\s]", "", regex=True)
    valid = s.str.fullmatch(r"-?(\d+(\.\d+)?|\.\d+)").fillna(False).astype(bool)
    values = pd.to_numeric(s.where(valid), errors="coerce")
    values = values.mask(negative & values.notna(), -values.abs())
    stats = {
        "missing": int(blank_to_na(raw, rules["null_tokens"]).isna().sum()),
        "unreadable": int((s.notna() & ~valid).sum()),
    }
    return values.astype("float64"), stats


# ------------------------------------------------------------ text and flags
def clean_text(series: pd.Series, rules: dict, how: str) -> pd.Series:
    s = blank_to_na(series, rules["null_tokens"]).str.replace(r"\s+", " ", regex=True)
    if how == "title":
        return s.str.title()
    if how == "lower":
        return s.str.lower()
    if how == "smart":  # only re-case names that are all lower or all upper; keep McDonald's as is
        flat = s.str.islower() | s.str.isupper()
        return s.mask(flat.fillna(False), s.str.title())
    return s


def clean_booleans(series: pd.Series, rules: dict) -> pd.Series:
    mapping = {t: True for t in rules["booleans"]["true_values"]}
    mapping.update({t: False for t in rules["booleans"]["false_values"]})
    s = blank_to_na(series, rules["null_tokens"]).str.lower()
    return s.map(mapping).astype("boolean")


def flag_outliers(amount: pd.Series, rules: dict) -> tuple[pd.Series, dict | None]:
    """Flag, never delete: a big real purchase and a decimal-point typo look the same."""
    cfg = rules["outliers"]
    values = amount.dropna()
    if len(values) < cfg["min_values"]:
        return pd.Series(False, index=amount.index), None
    q1, q3 = values.quantile([0.25, 0.75])
    spread = q3 - q1
    lower, upper = q1 - cfg["multiplier"] * spread, q3 + cfg["multiplier"] * spread
    flag = ((amount < lower) | (amount > upper)).fillna(False)
    return flag, {"lower": round(float(lower), 2), "upper": round(float(upper), 2)}


# ----------------------------------------------------------------- orchestration
def clean_frame(df: pd.DataFrame, mapping: dict, rules: dict, order: str) -> tuple[pd.DataFrame, dict]:
    out = df.drop(columns=["data_row"]).copy()
    date, date_stats = parse_dates(out[mapping["date"]], rules, order)
    amount, amount_stats = parse_amounts(out[mapping["amount"]], rules)
    flag, bounds = flag_outliers(amount, rules)

    out[mapping["date"]] = date.dt.strftime("%Y-%m-%d")
    out[mapping["amount"]] = amount

    by_name = {c.lower(): c for c in out.columns}
    text_rules = {"merchant": "smart", "category": "title", "account_type": "lower",
                  "notes": "plain", "transaction_id": "plain"}
    for name, how in text_rules.items():
        if name in by_name:
            out[by_name[name]] = clean_text(out[by_name[name]], rules, how)
    if "is_recurring" in by_name:
        out[by_name["is_recurring"]] = clean_booleans(out[by_name["is_recurring"]], rules)

    out["amount_outlier_flag"] = flag
    is_clean = date.notna() & amount.notna() & ~flag

    raw_date = blank_to_na(df[mapping["date"]], rules["null_tokens"])
    raw_amount = blank_to_na(df[mapping["amount"]], rules["null_tokens"])
    checks = [
        (raw_date.isna(), "Date is missing"),
        (raw_date.notna() & date.isna(), "Date couldn't be read"),
        (raw_amount.isna(), "Amount is missing"),
        (raw_amount.notna() & amount.isna(), "Amount couldn't be read"),
        (flag, "Unusual amount (flagged, kept)"),
    ]
    reasons = pd.Series("", index=out.index)
    for mask, text in checks:
        reasons = reasons.mask(mask, reasons + text + "; ")
    reasons = reasons.str.rstrip("; ")
    info = pd.DataFrame({
        "source_row": df["data_row"],
        "reason": reasons,
        "transaction_id": out[by_name["transaction_id"]] if "transaction_id" in by_name else None,
        "date_original": df[mapping["date"]],
        "amount_original": df[mapping["amount"]],
        "merchant": out[by_name["merchant"]] if "merchant" in by_name else None,
        "category": out[by_name["category"]] if "category" in by_name else None,
    })
    info = info[info["reason"] != ""]
    stats = {
        "dates": date_stats,
        "amounts": amount_stats,
        "outliers": int(flag.sum()),
        "outlier_bounds": bounds,
        "clean_rows": int(is_clean.sum()),
        "row_info": info,
    }
    return out, stats
