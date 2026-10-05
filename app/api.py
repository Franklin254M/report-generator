"""HTTP layer. No logic lives here: each route calls one JobStore method.

    pip install fastapi uvicorn python-multipart
    uvicorn app.api:create_app --factory --reload      # docs at http://127.0.0.1:8000/docs
"""
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.jobs import ApiError, JobStore


ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
SAMPLE_FILE = ROOT / "samples" / "messy_finance_transactions.csv"


class RerunBody(BaseModel):
    date_column: Optional[str] = None
    amount_column: Optional[str] = None
    date_order: Optional[Literal["month", "day"]] = None


def create_app(data_dir=None) -> FastAPI:
    store = JobStore(data_dir or os.environ.get("REPORT_DATA_DIR", "data/jobs"))

    @asynccontextmanager
    async def lifespan(_app):
        store.cleanup_expired()
        yield

    app = FastAPI(title="Report generator", version="0.3.0", lifespan=lifespan)

    @app.exception_handler(ApiError)
    async def api_error(_request, exc: ApiError):
        return JSONResponse(status_code=exc.status, content=exc.body())

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, exc: RequestValidationError):
        err = ApiError(422, "invalid_request", "The request wasn't in the expected format.",
                       "Check the field names and values against /docs.")
        return JSONResponse(status_code=422, content=err.body())

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/jobs", status_code=201)
    def create_job(file: UploadFile = File(...), date_order: Optional[str] = Form(None)):
        return store.create(file.file, file.filename, date_order)

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        return store.get(job_id)

    @app.post("/api/jobs/{job_id}/rerun")
    def rerun_job(job_id: str, body: RerunBody):
        return store.rerun(job_id, body.date_column, body.amount_column, body.date_order)

    @app.get("/api/jobs/{job_id}/files/{kind}")
    def download(job_id: str, kind: str):
        path, media_type, name = store.file(job_id, kind)
        return FileResponse(path, media_type=media_type, filename=name)

    @app.delete("/api/jobs/{job_id}", status_code=204)
    def delete_job(job_id: str):
        store.delete(job_id)
        return Response(status_code=204)

    @app.get("/sample.csv", include_in_schema=False)
    def sample():
        if not SAMPLE_FILE.is_file():
            raise ApiError(404, "file_not_available", "The sample file isn't available.",
                           "Upload your own CSV instead.")
        return FileResponse(SAMPLE_FILE, media_type="text/csv", filename="sample_transactions.csv")

    if WEB_DIR.is_dir():  # mounted last so every /api route wins
        app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")

    return app
