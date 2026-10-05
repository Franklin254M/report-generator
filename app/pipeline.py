"""Step 1: the whole pipeline as a command-line script.

    python -m app.pipeline samples/messy_finance_transactions.csv --out data/jobs/demo

Exit codes: 0 done, 2 needs_input (a column couldn't be found), 1 failed.
"""
import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from app.clean import clean_frame, detect_date_order, remove_duplicates
from app.ingest import PipelineError, read_csv
from app.profile import find_columns
from app.render import write_excel
from app.summarize import build_issues, quality_score

DEFAULT_RULES = Path(__file__).with_name("rules.yaml")
EXPIRY = timedelta(hours=24)
SOURCE_TEXT = {"chosen": "chosen by you", "detected": "detected from the file",
               "conflict": "file mixes both orders, default used", "assumed": "no evidence in the file, default used"}


def write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


OUTPUT_FILES = ("report.xlsx", "clean.csv", "log.md", "review_duplicates.csv")


def run(input_path, out_dir, *, date_order=None, date_column=None, amount_column=None,
        rules_path=DEFAULT_RULES, job_id=None, display_name=None, created_at=None) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in OUTPUT_FILES:  # a re-run must never leave files from the previous run behind
        (out_dir / name).unlink(missing_ok=True)
    rules = yaml.safe_load(Path(rules_path).read_text(encoding="utf-8"))
    order = date_order
    now = created_at or datetime.now(timezone.utc)
    job = {
        "id": job_id or str(uuid.uuid4()),
        "status": "running",
        "created_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + EXPIRY).isoformat(timespec="seconds"),
        "input": {"filename": display_name or Path(input_path).name},
        "rules_version": rules["rules_version"],
        "date_order": None,
        "date_order_source": None,
        "warnings": [],
        "stages": [],
        "quality_score": None,
        "issues": [],
        "needs_input": None,
        "files": [],
        "error": None,
    }
    try:
        df, meta = read_csv(input_path)
        job["input"].update(sha256=meta["sha256"], rows=meta["rows"])
        job["stages"].append({"name": "ingest", "rows_in": meta["rows"], "rows_out": meta["rows"]})

        mapping, needs_input = find_columns(df, rules, date_column, amount_column)
        if needs_input:
            job["status"], job["needs_input"] = "needs_input", needs_input
            write_json_atomic(out_dir / "job.json", job)
            return job

        if date_order:
            order, source = date_order, "chosen"
        else:
            order, source = detect_date_order(df[mapping["date"]], rules)
        job["date_order"], job["date_order_source"] = order, source

        df = df.reset_index(drop=True)
        df.insert(0, "data_row", df.index + 1)  # 1-based row number in the input, for the review file
        kept, exact_removed, review, id_removed = remove_duplicates(df)
        job["stages"].append({"name": "dedupe", "rows_in": len(df), "rows_out": len(kept),
                              "exact_duplicates_removed": exact_removed, "duplicate_ids_removed": id_removed})

        cleaned, stats = clean_frame(kept, mapping, rules, order)
        job["stages"].append({"name": "clean", "rows_in": len(kept), "rows_out": len(cleaned),
                              "flagged": stats["outliers"]})

        job["quality_score"] = quality_score(stats["clean_rows"], meta["rows"])
        job["issues"] = build_issues(exact_removed, id_removed, stats)
        job["columns"] = mapping

        cleaned.to_csv(out_dir / "clean.csv", index=False)
        job["files"] = ["clean.csv", "log.md"]
        if len(review):
            review.to_csv(out_dir / "review_duplicates.csv", index=False)
            job["files"].append("review_duplicates.csv")
        write_log(out_dir / "log.md", job, stats)
        try:
            write_excel(out_dir / "report.xlsx", job, cleaned, review, stats["row_info"], mapping)
            job["files"].insert(0, "report.xlsx")
        except Exception as exc:  # the CSV and log are still delivered
            job["warnings"].append(f"The Excel report couldn't be built ({type(exc).__name__}); CSV and log are ready.")
        job["status"] = "done"
    except PipelineError as err:
        job["status"] = "failed"
        job["error"] = {"code": err.code, "message": err.message, "fix": err.fix}
    write_json_atomic(out_dir / "job.json", job)
    return job


def write_log(path: Path, job: dict, stats: dict) -> None:
    lines = [f"# Cleaning log for {job['input']['filename']}", "",
             f"- Rules version {job['rules_version']}; slash dates read {job['date_order']}-first ({SOURCE_TEXT[job['date_order_source']]}).",
             f"- Input SHA-256: {job['input']['sha256']}"]
    for stage in job["stages"]:
        extras = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in stage.items()
                           if k not in ("name", "rows_in", "rows_out"))
        lines.append(f"- {stage['name']}: {stage['rows_in']} rows in, {stage['rows_out']} out"
                     + (f" ({extras})" if extras else ""))
    if stats["outlier_bounds"]:
        b = stats["outlier_bounds"]
        lines.append(f"- Outlier bounds (IQR): {b['lower']} to {b['upper']}; flagged rows were kept.")
    lines += ["", "## Issues"] + [f"- {i['note']}: {i['count']}" for i in job["issues"]]
    lines += ["", f"Quality score: {job['quality_score']} / 100 "
              "(share of input rows with a valid date, a valid amount, and no flag)."]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Clean a messy transactions CSV.")
    p.add_argument("input")
    p.add_argument("--out", default=None, help="Output folder (default data/jobs/<id>)")
    p.add_argument("--date-order", choices=["month", "day"], default=None)
    p.add_argument("--date-column")
    p.add_argument("--amount-column")
    args = p.parse_args(argv)
    job_id = str(uuid.uuid4())
    job = run(args.input, args.out or f"data/jobs/{job_id}", date_order=args.date_order,
              date_column=args.date_column, amount_column=args.amount_column, job_id=job_id)
    print(f"status: {job['status']}")
    if job["status"] == "done":
        print(f"quality score: {job['quality_score']} / 100")
        for i in job["issues"]:
            print(f"  {i['type']}: {i['count']}")
    elif job["status"] == "needs_input":
        n = job["needs_input"]
        print(f"needs {n['field']}; options: {', '.join(n['options'])}")
    else:
        print(f"{job['error']['message']} {job['error']['fix']}")
    return {"done": 0, "needs_input": 2}.get(job["status"], 1)


if __name__ == "__main__":
    sys.exit(main())
