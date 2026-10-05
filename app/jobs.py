"""Job service: everything the API does, with no web framework involved.

One folder per job: data/jobs/<uuid>/ holding input.csv, the outputs, and job.json.
The FastAPI layer (api.py) only translates HTTP to these functions, so this file is
the part that carries the logic and the tests.
"""
import json
import logging
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import ingest
from app.pipeline import run, write_json_atomic

log = logging.getLogger("report_generator")

TOMBSTONE_KEEP = timedelta(days=7)
STALE_PARTIAL = timedelta(days=1)
CHUNK = 64 * 1024
ERROR_STATUS = {"file_too_large": 413, "too_many_rows": 413, "empty_file": 422, "not_a_csv": 422}
FILES = {  # kind -> (file on disk, media type, download suffix)
    "xlsx": ("report.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "report.xlsx"),
    "csv": ("clean.csv", "text/csv", "clean.csv"),
    "log": ("log.md", "text/markdown", "log.md"),
    "review": ("review_duplicates.csv", "text/csv", "review.csv"),
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, fix: str):
        super().__init__(message)
        self.status, self.code, self.message, self.fix = status, code, message, fix

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "fix": self.fix}}


def safe_name(name) -> str:
    """Display/download name only. It is never used to build a path on disk."""
    base = Path(str(name or "").replace("\\", "/")).name
    base = re.sub(r"[^\w.\- ]", "_", base)[:120].strip(" .")
    return base or "upload.csv"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class JobStore:
    def __init__(self, data_dir):
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- helpers
    def _dir(self, job_id: str) -> Path:
        try:
            parsed = uuid.UUID(str(job_id), version=4)
        except ValueError:
            raise self._not_found() from None
        if str(parsed) != job_id:  # canonical lowercase form only
            raise self._not_found()
        return self.root / str(parsed)

    @staticmethod
    def _not_found() -> ApiError:
        return ApiError(404, "job_not_found", "We can't find that job.",
                        "Check the link, or upload the file again.")

    @staticmethod
    def _expired() -> ApiError:
        return ApiError(410, "job_expired", "That job has expired and its files were deleted.",
                        "Upload the file again.")

    def _load(self, folder: Path) -> dict:
        try:
            return json.loads((folder / "job.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise self._not_found() from None

    def _expire(self, folder: Path, job: dict) -> None:
        for item in folder.iterdir():
            if item.name != "job.json":
                shutil.rmtree(item, ignore_errors=True) if item.is_dir() else item.unlink(missing_ok=True)
        write_json_atomic(folder / "job.json", {"id": job["id"], "status": "expired",
                                                "expired_at": _now().isoformat(timespec="seconds")})

    def _public(self, job: dict) -> dict:
        view = dict(job)
        on_disk = {name for name in job.get("files", [])}
        view["downloads"] = {kind: f"/api/jobs/{job['id']}/files/{kind}"
                             for kind, (name, _, _) in FILES.items() if name in on_disk}
        return view

    # ----------------------------------------------------------------- API
    def create(self, stream, filename, date_order=None) -> dict:
        date_order = date_order or None  # an empty form field (as sent by /docs or curl -F) means "not given"
        self._check_order(date_order)
        self.cleanup_expired()
        job_id = str(uuid.uuid4())
        folder = self.root / job_id
        folder.mkdir()
        try:
            total = 0
            with open(folder / "input.csv", "wb") as out:
                while chunk := stream.read(CHUNK):
                    total += len(chunk)
                    if total > ingest.MAX_BYTES:
                        raise ApiError(413, "file_too_large", "That file is over 10 MB.",
                                       "Split it into smaller files.")
                    out.write(chunk)
            job = run(folder / "input.csv", folder, date_order=date_order, job_id=job_id,
                      display_name=safe_name(filename))
            if job["status"] == "failed":
                err = job["error"]
                raise ApiError(ERROR_STATUS.get(err["code"], 422), err["code"], err["message"], err["fix"])
            return self._public(job)
        except ApiError:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        except Exception:
            log.exception("job %s crashed", job_id)
            shutil.rmtree(folder, ignore_errors=True)
            raise ApiError(500, "internal_error", "Something went wrong on our side.",
                           "Try again. If it keeps happening, tell Frank.") from None

    def get(self, job_id: str) -> dict:
        folder = self._dir(job_id)
        if not folder.is_dir():
            raise self._not_found()
        job = self._load(folder)
        if job.get("status") == "expired":
            raise self._expired()
        if datetime.fromisoformat(job["expires_at"]) < _now():
            self._expire(folder, job)
            raise self._expired()
        return self._public(job)

    def rerun(self, job_id: str, date_column=None, amount_column=None, date_order=None) -> dict:
        date_order, date_column, amount_column = date_order or None, date_column or None, amount_column or None
        self._check_order(date_order)
        job = self.get(job_id)
        folder = self._dir(job_id)
        try:
            new = run(folder / "input.csv", folder, date_order=date_order, date_column=date_column,
                      amount_column=amount_column, job_id=job_id, display_name=job["input"]["filename"],
                      created_at=datetime.fromisoformat(job["created_at"]))
        except Exception:
            log.exception("rerun of %s crashed", job_id)
            raise ApiError(500, "internal_error", "Something went wrong on our side.",
                           "Try again. If it keeps happening, tell Frank.") from None
        if new["status"] == "failed":
            err = new["error"]
            raise ApiError(ERROR_STATUS.get(err["code"], 422), err["code"], err["message"], err["fix"])
        return self._public(new)

    def file(self, job_id: str, kind: str):
        job = self.get(job_id)
        if kind == "pdf":
            raise ApiError(404, "file_not_available", "PDF reports aren't available yet.",
                           "Download the Excel report instead.")
        if kind not in FILES:  # whitelist: nothing else can ever be served
            raise ApiError(404, "unknown_file", "That isn't a file we produce.",
                           "Use one of: " + ", ".join(FILES))
        name, media_type, suffix = FILES[kind]
        path = self._dir(job_id) / name
        if name not in job.get("files", []) or not path.is_file():
            raise ApiError(404, "file_not_available", "That file isn't available for this job.",
                           "Check the job status for what was produced.")
        stem = Path(job["input"]["filename"]).stem
        return path, media_type, f"{stem}_{suffix}"

    def delete(self, job_id: str) -> None:
        folder = self._dir(job_id)
        if not folder.is_dir():
            raise self._not_found()
        shutil.rmtree(folder, ignore_errors=True)

    def cleanup_expired(self) -> int:
        """Delete expired jobs' files (keeping a small marker so clients get 'expired', not 'not found'),
        then remove old markers and abandoned partial uploads."""
        removed = 0
        for folder in self.root.iterdir():
            if not folder.is_dir():
                continue
            try:
                uuid.UUID(folder.name, version=4)
                job = json.loads((folder / "job.json").read_text(encoding="utf-8"))
            except (ValueError, OSError):
                age = _now() - datetime.fromtimestamp(folder.stat().st_mtime, timezone.utc)
                if age > STALE_PARTIAL:
                    shutil.rmtree(folder, ignore_errors=True)
                    removed += 1
                continue
            if job.get("status") == "expired":
                if _now() - datetime.fromisoformat(job["expired_at"]) > TOMBSTONE_KEEP:
                    shutil.rmtree(folder, ignore_errors=True)
            elif datetime.fromisoformat(job["expires_at"]) < _now():
                self._expire(folder, job)
                removed += 1
        return removed

    @staticmethod
    def _check_order(date_order) -> None:
        if date_order not in (None, "month", "day"):
            raise ApiError(422, "invalid_request", "date_order must be 'month' or 'day'.",
                           "Send one of those two values, or leave it out.")
