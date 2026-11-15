"""Async HTTP intake and run status for the procurement agent."""

import os
from base64 import b64decode
from binascii import Error as Base64Error
from hmac import compare_digest
from uuid import UUID

import psycopg
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from backend.contracts import AcceptedRun, EmailIntake, IntakeMetadata, RunStatus
from backend.store import IngestionStore

DEFAULT_DATABASE_URL = "postgresql://procurement:procurement@127.0.0.1:5432/procurement"
MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_REQUEST_BYTES = 4 * ((MAX_PDF_BYTES + 2) // 3) + 64 * 1024

INGEST_SCHEMA = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file"],
                    "properties": {
                        "file": {"type": "string", "format": "binary"},
                        **IntakeMetadata.model_json_schema()["properties"],
                    },
                },
            },
            "application/json": {"schema": EmailIntake.model_json_schema()},
        },
    },
}


async def bounded_body(request: Request) -> bytes:
    advertised = request.headers.get("content-length")
    if advertised is not None and advertised.isascii() and advertised.isdecimal():
        if len(advertised) > 20 or int(advertised) > MAX_REQUEST_BYTES:
            raise HTTPException(413, "Intake request is too large.")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_REQUEST_BYTES:
            raise HTTPException(413, "Intake request is too large.")
        body.extend(chunk)
    return bytes(body)


async def intake_payload(request: Request, webhook_token: str | None) -> tuple[bytes, IntakeMetadata]:
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type not in {"multipart/form-data", "application/json"}:
        raise HTTPException(415, "Use multipart/form-data or application/json.")
    if media_type == "application/json":
        if not webhook_token:
            raise HTTPException(403, "Email intake is disabled.")
        expected = f"Bearer {webhook_token}".encode()
        received = request.headers.get("authorization", "").encode()
        if not compare_digest(received, expected):
            raise HTTPException(401, "Invalid email intake credential.", {"WWW-Authenticate": "Bearer"})

    body = await bounded_body(request)
    try:
        if media_type == "application/json":
            email = EmailIntake.model_validate_json(body)
            pdf = b64decode(email.pdf_base64, validate=True)
            metadata = IntakeMetadata.model_validate(email.model_dump(exclude={"pdf_base64"}))
        else:
            # Parse multipart only after enforcing a bound on the raw request stream.
            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}

            buffered = Request(request.scope, receive=receive)
            async with buffered.form(max_files=1, max_fields=3, max_part_size=4096) as form:
                files = form.getlist("file")
                if len(files) != 1 or not isinstance(files[0], UploadFile):
                    raise HTTPException(422, "A PDF file field named 'file' is required.")
                metadata_fields = {}
                for key, value in form.multi_items():
                    if key == "file":
                        continue
                    if not isinstance(value, str) or key in metadata_fields:
                        raise HTTPException(422, "Invalid or repeated intake field.")
                    metadata_fields[key] = value
                metadata = IntakeMetadata.model_validate(metadata_fields)
                pdf = await files[0].read(MAX_PDF_BYTES + 1)
    except (ValidationError, Base64Error):
        raise HTTPException(422, "Invalid intake metadata or attachment encoding.") from None

    if len(pdf) > MAX_PDF_BYTES:
        raise HTTPException(413, "PDF exceeds 10 MiB.")
    if not pdf.startswith(b"%PDF-"):
        raise HTTPException(422, "Attachment must start with a PDF header.")
    return pdf, metadata


def create_app(database_url: str | None = None, webhook_token: str | None = None) -> FastAPI:
    store = IngestionStore(database_url or os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL)
    email_token = webhook_token if webhook_token is not None else os.environ.get("INGEST_WEBHOOK_TOKEN")
    app = FastAPI(title="Procurement intake", version="0.3.0")

    @app.post(
        "/api/v1/ingest",
        status_code=202,
        response_model=AcceptedRun,
        responses={200: {"model": AcceptedRun, "description": "Duplicate PDF"}},
        openapi_extra=INGEST_SCHEMA,
    )
    async def ingest(request: Request, response: Response) -> AcceptedRun:
        pdf, metadata = await intake_payload(request, email_token)
        try:
            result = await store.register(pdf, metadata)
        except psycopg.Error:
            raise HTTPException(503, "Procurement intake storage is unavailable.") from None
        if result.status == "duplicate_ignored":
            response.status_code = 200
        return result

    @app.get("/api/v1/runs/{run_id}", response_model=RunStatus)
    async def get_run(run_id: UUID) -> RunStatus:
        try:
            result = await store.get_run(run_id)
        except psycopg.Error:
            raise HTTPException(503, "Procurement intake storage is unavailable.") from None
        if result is None:
            raise HTTPException(404, "Procurement run was not found.")
        return result

    return app
