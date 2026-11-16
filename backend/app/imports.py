from __future__ import annotations

import csv
import hashlib
import io
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import audit
from app.models import (
    ConsentRecord,
    Contact,
    ImportBatch,
    ImportStatus,
    Suppression,
    normalize_email,
)

MAX_FILE_BYTES = 1_000_000
MAX_ROWS = 5_000
MAX_COLUMNS = 100
MAX_CELL_CHARS = 5_000
REQUIRED_MAPPING = {
    "email",
    "consent_source",
    "consented_at",
    "consent_purpose",
    "consent_provenance",
}
OPTIONAL_MAPPING = {"display_name"}


class ColumnMapping(BaseModel):
    email: str = Field(min_length=1, max_length=100)
    consent_source: str = Field(min_length=1, max_length=100)
    consented_at: str = Field(min_length=1, max_length=100)
    consent_purpose: str = Field(min_length=1, max_length=100)
    consent_provenance: str = Field(min_length=1, max_length=100)
    display_name: str | None = Field(default=None, max_length=100)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def read_csv(upload: UploadFile) -> tuple[str, list[str], list[dict[str, str]]]:
    filename = (upload.filename or "").replace("\\", "/").split("/")[-1]
    if not filename.lower().endswith(".csv"):
        raise HTTPException(415, "Upload a .csv file")
    raw = await upload.read(MAX_FILE_BYTES + 1)
    await upload.close()
    if len(raw) > MAX_FILE_BYTES:
        raise HTTPException(413, "CSV exceeds the 1 MB limit")
    try:
        text = raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise HTTPException(400, "CSV must use UTF-8 encoding") from exc
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        headers = [header.strip() for header in (reader.fieldnames or [])]
        if not headers or len(headers) > MAX_COLUMNS or any(not h or len(h) > 100 for h in headers):
            raise HTTPException(400, "CSV must have 1 to 100 non-empty headers")
        if len(set(headers)) != len(headers):
            raise HTTPException(400, "CSV headers must be unique")
        reader.fieldnames = headers
        rows: list[dict[str, str]] = []
        for row in reader:
            if len(rows) >= MAX_ROWS:
                raise HTTPException(413, "CSV exceeds the 5,000 row limit")
            if None in row:
                raise HTTPException(400, "CSV row has more values than the header")
            if any(value is None for value in row.values()):
                raise HTTPException(400, "CSV row has fewer values than the header")
            values = {key: value or "" for key, value in row.items() if key is not None}
            if any(len(value) > MAX_CELL_CHARS for value in values.values()):
                raise HTTPException(400, "CSV cell exceeds the 5,000 character limit")
            rows.append(values)
    except csv.Error as exc:
        raise HTTPException(400, "Malformed CSV") from exc
    return filename or "upload.csv", headers, rows


def validate_mapping(mapping: ColumnMapping, headers: list[str]) -> dict[str, str | None]:
    values = mapping.model_dump()
    selected = [value for value in values.values() if value]
    if any(column not in headers for column in selected):
        raise HTTPException(422, "Every mapped column must exist in the uploaded CSV")
    if len(set(selected)) != len(selected):
        raise HTTPException(422, "Each CSV column can map to only one field")
    return values


def _validate_row(
    row: dict[str, str], mapping: dict[str, str | None]
) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []

    def value(field: str) -> str:
        column = mapping.get(field)
        return row.get(column, "").strip() if column else ""

    raw_email = value("email")
    try:
        normalized = normalize_email(raw_email)
    except ValueError:
        normalized = ""
        errors.append("invalid_email")
    source = value("consent_source")
    purpose = value("consent_purpose")
    provenance = value("consent_provenance")
    timestamp = value("consented_at")
    if not source:
        errors.append("missing_consent_source")
    if not purpose:
        errors.append("missing_consent_purpose")
    if not provenance:
        errors.append("missing_consent_provenance")
    try:
        consented_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if consented_at.tzinfo is None:
            raise ValueError
        if consented_at > datetime.now(UTC):
            raise ValueError
    except ValueError:
        consented_at = None
        errors.append("invalid_consent_timestamp")
    if errors:
        return None, errors
    return {
        "email": raw_email.strip(),
        "email_normalized": normalized,
        "display_name": value("display_name") or None,
        "source": source,
        "consented_at": consented_at.isoformat(),
        "purpose": purpose,
        "provenance": provenance,
    }, []


def preview_rows(
    rows: list[dict[str, str]], mapping: dict[str, str | None], db: Session
) -> tuple[list[dict[str, Any]], dict[str, int], list[dict[str, Any]]]:
    existing = set(db.scalars(select(Contact.email_normalized)))
    suppressed = set(db.scalars(select(Suppression.email_normalized)))
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    snapshot: list[dict[str, Any]] = []
    counts = {"valid": 0, "invalid": 0, "duplicate": 0, "suppressed": 0}
    for index, row in enumerate(rows, start=2):
        cleaned, errors = _validate_row(row, mapping)
        if cleaned is None:
            counts["invalid"] += 1
            result.append({"row": index, "status": "invalid", "errors": errors})
            continue
        address = cleaned["email_normalized"]
        if address in suppressed:
            counts["suppressed"] += 1
            result.append({"row": index, "status": "suppressed", "errors": ["globally_suppressed"]})
        elif address in existing or address in seen:
            counts["duplicate"] += 1
            result.append(
                {
                    "row": index,
                    "status": "duplicate",
                    "errors": [
                        "contact_already_exists" if address in existing else "duplicate_in_file"
                    ],
                }
            )
        else:
            seen.add(address)
            counts["valid"] += 1
            snapshot.append(cleaned)
            result.append({"row": index, "status": "valid", "errors": []})
    return result, counts, snapshot


def commit_batch(batch: ImportBatch, token: str, db: Session) -> dict[str, int]:
    batch = db.scalar(select(ImportBatch).where(ImportBatch.id == batch.id).with_for_update())
    if batch is None:
        raise HTTPException(404, "Import preview not found")
    expires_at = batch.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if (
        batch.status != ImportStatus.PREVIEWED
        or not batch.commit_token_hash
        or expires_at <= datetime.now(UTC)
    ):
        raise HTTPException(409, "Import preview is no longer available")
    if not secrets.compare_digest(batch.commit_token_hash, _token_hash(token)):
        raise HTTPException(403, "Invalid import commit token")
    existing = set(db.scalars(select(Contact.email_normalized)))
    suppressed = set(db.scalars(select(Suppression.email_normalized)))
    created = 0
    skipped = 0
    for item in batch.rows_snapshot:
        address = item["email_normalized"]
        if address in existing or address in suppressed:
            skipped += 1
            continue
        contact = Contact(
            email=item["email"], email_normalized=address, display_name=item["display_name"]
        )
        db.add(contact)
        try:
            db.flush()
        except IntegrityError as exc:
            raise HTTPException(
                409, "Contact data changed during commit; create a fresh preview"
            ) from exc
        db.add(
            ConsentRecord(
                contact_id=contact.id,
                source=item["source"],
                consented_at=datetime.fromisoformat(item["consented_at"]),
                purpose=item["purpose"],
                provenance=item["provenance"],
            )
        )
        existing.add(address)
        created += 1
    batch.status = ImportStatus.COMMITTED
    batch.accepted_count = created
    batch.rejected_count = batch.row_count - created
    batch.commit_token_hash = ""
    batch.rows_snapshot = []
    audit(
        db, "imports.committed", detail=f"batch_id={batch.id};created={created};skipped={skipped}"
    )
    return {"created": created, "skipped_at_commit": skipped}


def make_batch(
    filename: str, rows: list[dict[str, str]], mapping: dict[str, str | None], db: Session
) -> tuple[ImportBatch, str, list[dict[str, Any]], dict[str, int]]:
    db.execute(
        delete(ImportBatch).where(
            ImportBatch.status == ImportStatus.PREVIEWED,
            ImportBatch.expires_at <= datetime.now(UTC),
        )
    )
    report, counts, snapshot = preview_rows(rows, mapping, db)
    token = secrets.token_urlsafe(32)
    batch = ImportBatch(
        filename=filename,
        status=ImportStatus.PREVIEWED,
        row_count=len(rows),
        accepted_count=counts["valid"],
        rejected_count=len(rows) - counts["valid"],
        commit_token_hash=_token_hash(token),
        rows_snapshot=snapshot,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    db.add(batch)
    db.flush()
    return batch, token, report, counts
