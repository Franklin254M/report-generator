"""Read a CSV safely: size limits, encoding, delimiter. Everything stays text."""
import csv
import hashlib
import io
from pathlib import Path

import pandas as pd

MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 200_000


class PipelineError(Exception):
    def __init__(self, code: str, message: str, fix: str):
        super().__init__(message)
        self.code, self.message, self.fix = code, message, fix


def read_csv(path) -> tuple[pd.DataFrame, dict]:
    path = Path(path)
    if not path.exists():
        raise PipelineError("file_not_found", f"{path.name} doesn't exist.", "Check the file path.")
    if path.stat().st_size > MAX_BYTES:
        raise PipelineError("file_too_large", "That file is over 10 MB.", "Split it into smaller files.")
    raw = path.read_bytes()
    if not raw.strip():
        raise PipelineError("empty_file", "That file is empty.", "Choose a file with a header row and data.")
    if b"\x00" in raw:
        raise PipelineError("not_a_csv", "That doesn't look like a CSV file.", "Export the data as CSV and try again.")

    try:
        text, encoding = raw.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        text, encoding = raw.decode("latin-1"), "latin-1"

    try:
        delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
    except csv.Error:
        delimiter = ","

    try:
        df = pd.read_csv(io.StringIO(text), sep=delimiter, dtype=str, keep_default_na=False, na_values=[""])
    except (pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
        raise PipelineError("not_a_csv", "Couldn't read that file as a CSV.", "Check it opens correctly in Excel.") from exc

    df.columns = [str(c).strip() for c in df.columns]
    if df.empty:
        raise PipelineError("empty_file", "That file has a header but no rows.", "Choose a file with data.")
    if len(df) > MAX_ROWS:
        raise PipelineError("too_many_rows", f"That file has more than {MAX_ROWS:,} rows.", "Split it into smaller files.")

    meta = {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "rows": len(df),
        "encoding": encoding,
        "delimiter": delimiter,
    }
    return df, meta
