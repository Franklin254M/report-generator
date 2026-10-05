"""HTTP tests. They skip themselves unless FastAPI and httpx are installed:
    pip install fastapi uvicorn python-multipart httpx
    python -m unittest tests.test_api_http -v
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path

HAVE_WEB = all(importlib.util.find_spec(m) for m in ("fastapi", "httpx", "multipart"))
SAMPLE = Path(__file__).resolve().parent / "messy_sample.csv"


@unittest.skipUnless(HAVE_WEB, "fastapi, httpx and python-multipart not installed")
class HttpTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from app.api import create_app
        self._tmp = tempfile.TemporaryDirectory()
        self.client = TestClient(create_app(Path(self._tmp.name) / "jobs"))
        self.client.__enter__()  # runs startup cleanup

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self._tmp.cleanup()

    def upload(self, data=None, name="sample.csv", **form):
        data = SAMPLE.read_bytes() if data is None else data
        return self.client.post("/api/jobs", files={"file": (name, data, "text/csv")}, data=form)

    def assertError(self, response, status, code):
        self.assertEqual(response.status_code, status)
        err = response.json()["error"]
        self.assertEqual(err["code"], code)
        self.assertTrue(err["message"] and err["fix"])

    def test_health_and_docs(self):
        self.assertEqual(self.client.get("/api/health").json(), {"status": "ok"})
        self.assertEqual(self.client.get("/docs").status_code, 200)

    def test_upload_status_and_download(self):
        r = self.upload()
        self.assertEqual(r.status_code, 201)
        job = r.json()
        self.assertEqual(job["status"], "done")
        got = self.client.get(f"/api/jobs/{job['id']}")
        self.assertEqual(got.status_code, 200)
        csv = self.client.get(job["downloads"]["csv"])
        self.assertEqual(csv.status_code, 200)
        self.assertTrue(csv.headers["content-type"].startswith("text/csv"))
        self.assertIn("sample_clean.csv", csv.headers["content-disposition"])
        self.assertTrue(csv.text.startswith("transaction_id,date"))
        xlsx = self.client.get(job["downloads"]["xlsx"])
        self.assertEqual(xlsx.content[:2], b"PK")  # a real zip-based workbook

    def test_front_end_and_sample_are_served(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("text/html", page.headers["content-type"])
        self.assertIn("Data cleaner", page.text)
        self.assertEqual(self.client.get("/app.js").status_code, 200)
        self.assertEqual(self.client.get("/style.css").status_code, 200)
        sample = self.client.get("/sample.csv")
        self.assertEqual(sample.status_code, 200)
        self.assertTrue(sample.text.startswith("transaction_id"))
        self.assertEqual(self.client.get("/api/health").status_code, 200)  # API still wins over the static mount

    def test_empty_date_order_field_is_accepted(self):
        self.assertEqual(self.upload(date_order="").status_code, 201)

    def test_errors_share_one_shape(self):
        self.assertError(self.client.get("/api/jobs/not-a-real-id"), 404, "job_not_found")
        self.assertError(self.upload(b""), 422, "empty_file")
        self.assertError(self.upload(b"PK\x00\x03"), 422, "not_a_csv")
        self.assertError(self.upload(date_order="weekly"), 422, "invalid_request")
        self.assertError(self.client.post("/api/jobs"), 422, "invalid_request")  # no file field

    def test_needs_input_then_rerun(self):
        job = self.upload(b"date,merchant,total\n2023-01-01,A,5\n").json()
        self.assertEqual(job["status"], "needs_input")
        bad = self.client.post(f"/api/jobs/{job['id']}/rerun", json={"date_order": "weekly"})
        self.assertError(bad, 422, "invalid_request")
        ok = self.client.post(f"/api/jobs/{job['id']}/rerun", json={"amount_column": "total"})
        self.assertEqual(ok.json()["status"], "done")

    def test_pdf_not_yet_and_delete(self):
        job = self.upload().json()
        self.assertError(self.client.get(f"/api/jobs/{job['id']}/files/pdf"), 404, "file_not_available")
        self.assertEqual(self.client.delete(f"/api/jobs/{job['id']}").status_code, 204)
        self.assertError(self.client.get(f"/api/jobs/{job['id']}"), 404, "job_not_found")


if __name__ == "__main__":
    unittest.main()
