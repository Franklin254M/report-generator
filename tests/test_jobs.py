"""Service-layer tests: the API's logic with no web framework. Run: python -m unittest discover -s tests -t ."""
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app import ingest
from app.jobs import ApiError, JobStore, safe_name

SAMPLE = Path(__file__).resolve().parent / "messy_sample.csv"
NO_AMOUNT = b"date,merchant,total\n2023-01-01,A,5\n2023-01-02,B,7\n"


class JobStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = JobStore(self.root / "jobs")

    def tearDown(self):
        self._tmp.cleanup()

    def upload(self, data=None, name="sample.csv", **kw):
        return self.store.create(io.BytesIO(SAMPLE.read_bytes() if data is None else data), name, **kw)

    def folders(self):
        return [p for p in (self.root / "jobs").iterdir() if p.is_dir()]

    def force_expiry(self, job_id, when):
        path = self.root / "jobs" / job_id / "job.json"
        data = json.loads(path.read_text())
        data["expires_at"] = when.isoformat(timespec="seconds")
        path.write_text(json.dumps(data))

    def assertApiError(self, status, code, fn, *args, **kw):
        with self.assertRaises(ApiError) as ctx:
            fn(*args, **kw)
        self.assertEqual((ctx.exception.status, ctx.exception.code), (status, code))
        body = ctx.exception.body()["error"]
        self.assertTrue(body["message"] and body["fix"])  # every error says what to do next

    # ---------------------------------------------------------------- upload
    def test_upload_returns_done_job_with_downloads(self):
        job = self.upload()
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["input"]["filename"], "sample.csv")
        self.assertEqual(set(job["downloads"]), {"xlsx", "csv", "log", "review"})
        self.assertTrue(job["downloads"]["csv"].endswith(f"/api/jobs/{job['id']}/files/csv"))
        self.assertEqual(len(self.folders()), 1)

    def test_hostile_filename_never_touches_disk_paths(self):
        job = self.upload(name="../../etc/evil name?.csv")
        self.assertEqual(job["input"]["filename"], "evil name_.csv")
        self.assertEqual([p.name for p in self.root.iterdir()], ["jobs"])  # nothing escaped the data dir
        self.assertEqual(safe_name(""), "upload.csv")
        self.assertEqual(safe_name("C:\\Users\\me\\bank.csv"), "bank.csv")

    def test_too_large_is_rejected_while_streaming_and_leaves_nothing(self):
        with mock.patch.object(ingest, "MAX_BYTES", 100):
            self.assertApiError(413, "file_too_large", self.upload, b"a,b\n" * 200)
        self.assertEqual(self.folders(), [])

    def test_empty_and_binary_files_are_rejected_and_leave_nothing(self):
        self.assertApiError(422, "empty_file", self.upload, b"")
        self.assertApiError(422, "not_a_csv", self.upload, b"PK\x00\x03binary")
        self.assertEqual(self.folders(), [])

    def test_invalid_date_order_is_rejected(self):
        self.assertApiError(422, "invalid_request", self.upload, date_order="weekly")
        self.assertEqual(self.folders(), [])

    def test_empty_form_values_mean_not_given(self):
        job = self.upload(date_order="")
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["date_order_source"], "detected")
        again = self.store.rerun(job["id"], date_column="", amount_column="", date_order="")
        self.assertEqual(again["status"], "done")

    def test_unexpected_crash_returns_clean_500_and_leaves_nothing(self):
        with mock.patch("app.jobs.run", side_effect=RuntimeError("boom")):
            self.assertApiError(500, "internal_error", self.upload)
        self.assertEqual(self.folders(), [])

    # ------------------------------------------------------- needs_input flow
    def test_missing_column_then_rerun_keeps_id_and_expiry(self):
        job = self.upload(NO_AMOUNT)
        self.assertEqual(job["status"], "needs_input")
        self.assertEqual(job["needs_input"]["field"], "amount_column")
        self.assertEqual(job["downloads"], {})
        done = self.store.rerun(job["id"], amount_column="total")
        self.assertEqual((done["status"], done["id"], done["expires_at"]),
                         ("done", job["id"], job["expires_at"]))
        self.assertEqual(done["created_at"], job["created_at"])
        self.assertEqual(done["input"]["filename"], "sample.csv")
        self.assertIn("csv", done["downloads"])

    def test_rerun_with_unknown_column_asks_again(self):
        job = self.upload(NO_AMOUNT)
        again = self.store.rerun(job["id"], amount_column="nope")
        self.assertEqual(again["status"], "needs_input")

    def test_rerun_with_date_order_changes_output_and_drops_stale_files(self):
        job = self.upload()
        self.assertIn("review", job["downloads"])
        month = (self.root / "jobs" / job["id"] / "clean.csv").read_text()
        again = self.store.rerun(job["id"], date_order="day")
        day = (self.root / "jobs" / job["id"] / "clean.csv").read_text()
        self.assertNotEqual(month, day)
        self.assertEqual(again["date_order_source"], "chosen")

    # ---------------------------------------------------------------- lookups
    def test_unknown_and_malformed_ids_are_404(self):
        for bad in ("../etc/passwd", "123", "", "A" * 36, "00000000-0000-0000-0000-000000000000",
                    str(self.root)):
            self.assertApiError(404, "job_not_found", self.store.get, bad)
        self.assertApiError(404, "job_not_found", self.store.get, "3f2b8c1e-5d4a-4b7e-9c11-2a6f0d9e8b73")

    def test_get_returns_same_job(self):
        job = self.upload()
        self.assertEqual(self.store.get(job["id"])["quality_score"], job["quality_score"])

    # ------------------------------------------------------------- downloads
    def test_downloads_use_whitelist_and_friendly_names(self):
        job = self.upload()
        path, media, name = self.store.file(job["id"], "csv")
        self.assertEqual((path.name, media, name), ("clean.csv", "text/csv", "sample_clean.csv"))
        self.assertEqual(self.store.file(job["id"], "xlsx")[2], "sample_report.xlsx")
        self.assertApiError(404, "file_not_available", self.store.file, job["id"], "pdf")
        for sneaky in ("job.json", "../job.json", "input", "input.csv", "..%2Fjob.json"):
            self.assertApiError(404, "unknown_file", self.store.file, job["id"], sneaky)

    def test_review_file_missing_when_there_were_no_duplicates(self):
        clean = b"date,amount\n2023-01-01,5\n2023-01-02,6\n"
        job = self.upload(clean)
        self.assertApiError(404, "file_not_available", self.store.file, job["id"], "review")

    # ------------------------------------------------------------- delete/expiry
    def test_delete_removes_everything(self):
        job = self.upload()
        self.store.delete(job["id"])
        self.assertEqual(self.folders(), [])
        self.assertApiError(404, "job_not_found", self.store.get, job["id"])
        self.assertApiError(404, "job_not_found", self.store.delete, job["id"])

    def test_expired_job_returns_410_and_files_are_gone(self):
        job = self.upload()
        self.force_expiry(job["id"], datetime.now(timezone.utc) - timedelta(minutes=1))
        self.assertApiError(410, "job_expired", self.store.get, job["id"])
        self.assertEqual([p.name for p in (self.root / "jobs" / job["id"]).iterdir()], ["job.json"])
        self.assertApiError(410, "job_expired", self.store.get, job["id"])  # still 410, not 404
        self.assertApiError(410, "job_expired", self.store.file, job["id"], "csv")

    def test_cleanup_expires_old_jobs_and_drops_old_markers(self):
        keep, old, older = self.upload(), self.upload(), self.upload()
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        self.force_expiry(old["id"], past)
        self.force_expiry(older["id"], past)
        self.assertEqual(self.store.cleanup_expired(), 2)
        self.assertEqual(self.store.get(keep["id"])["status"], "done")
        marker = self.root / "jobs" / older["id"] / "job.json"
        data = json.loads(marker.read_text())
        data["expired_at"] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        marker.write_text(json.dumps(data))
        self.store.cleanup_expired()
        self.assertApiError(404, "job_not_found", self.store.get, older["id"])
        self.assertApiError(410, "job_expired", self.store.get, old["id"])

    def test_cleanup_removes_abandoned_partial_uploads(self):
        stale = self.root / "jobs" / "3f2b8c1e-5d4a-4b7e-9c11-2a6f0d9e8b73"
        stale.mkdir()
        (stale / "input.csv").write_text("partial")
        old = (datetime.now(timezone.utc) - timedelta(days=2)).timestamp()
        import os
        os.utime(stale, (old, old))
        self.store.cleanup_expired()
        self.assertFalse(stale.exists())

    def test_ids_are_unique_per_upload(self):
        self.assertNotEqual(self.upload()["id"], self.upload()["id"])


if __name__ == "__main__":
    unittest.main()
