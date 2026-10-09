"""Admin Memory / Q&A import from the dashboard: parse, validate, preview, commit.

Rules
* Upload types: ``.json`` (array or {"records": [...]}) and, for memory only,
  ``.txt`` (one memory per non-empty line). Max 1 MB, max 2000 records.
* Content is plain text. It is stored and later shown escaped; it is never
  executed or rendered as HTML.
* Duplicates are detected with the same deterministic ids the Telegram import
  uses (``database._import_id``), so re-importing the same file adds nothing.
* Commit always writes a ``memory_import_batches`` row for provenance.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

import database
from utils.logger import get_logger

logger = get_logger(__name__)

MAX_BYTES = 1_000_000
MAX_RECORDS = 2000
MAX_TEXT = 2000
ALLOWED_EXTENSIONS = {".json", ".txt"}


class ImportProblem(ValueError):
    pass


def _parse_date(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ImportProblem(f"invalid date: {value[:40]!r}") from exc
        return parsed
    raise ImportProblem("date must be an ISO string")


def _clean_text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ImportProblem(f"{field} must be text")
    text = value.replace("\x00", "").strip()
    if not text:
        raise ImportProblem(f"{field} is empty")
    if len(text) > MAX_TEXT:
        raise ImportProblem(f"{field} is longer than {MAX_TEXT} characters")
    return text


def parse_upload(filename: str, data: bytes, kind: str) -> tuple[list[dict[str, Any]], str]:
    """Return ``(records, sha256)``. Raises :class:`ImportProblem` with a user-safe message."""
    if kind not in ("memory", "qa"):
        raise ImportProblem("unknown import kind")
    name = (filename or "").lower()
    ext = next((e for e in ALLOWED_EXTENSIONS if name.endswith(e)), None)
    if ext is None:
        raise ImportProblem("Only .json or .txt files are accepted.")
    if ext == ".txt" and kind != "memory":
        raise ImportProblem("Q&A import needs a .json file with question and answer fields.")
    if not data:
        raise ImportProblem("The file is empty.")
    if len(data) > MAX_BYTES:
        raise ImportProblem("The file is larger than 1 MB.")
    if b"\x00" in data:
        raise ImportProblem("Binary content is not accepted.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportProblem("The file must be UTF-8 text.") from exc
    digest = hashlib.sha256(data).hexdigest()

    if ext == ".txt":
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            raise ImportProblem("No text lines found.")
        if len(lines) > MAX_RECORDS:
            raise ImportProblem(f"At most {MAX_RECORDS} records per file.")
        records = [{"text": _clean_text(line, "line")} for line in lines]
        return records, digest

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ImportProblem(f"JSON error at line {exc.lineno}, column {exc.colno}.") from exc
    if isinstance(payload, dict):
        payload = payload.get("records")
    if not isinstance(payload, list) or not payload:
        raise ImportProblem("Provide a non-empty JSON array of records.")
    if len(payload) > MAX_RECORDS:
        raise ImportProblem(f"At most {MAX_RECORDS} records per file.")
    records: list[dict[str, Any]] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise ImportProblem(f"Record {index} must be a JSON object.")
        if kind == "memory":
            raw_text = item.get("text", item.get("message"))
            record = {"text": _clean_text(raw_text, f"record {index} text")}
        else:
            record = {
                "question": _clean_text(item.get("question"), f"record {index} question"),
                "answer": _clean_text(item.get("answer"), f"record {index} answer"),
            }
        date = _parse_date(item.get("date"))
        if date is not None:
            record["date"] = date
        records.append(record)
    return records, digest


def _record_ids(kind: str, chat_id: int, admin_id: int, records: list[dict[str, Any]]) -> list[tuple[dict, int]]:
    """Deterministic ids. Memory ids depend on the importing admin (as in database.import_admin_memory)."""
    out = []
    for record in records:
        if kind == "memory":
            text = record["text"]
            fp = hashlib.sha256(f"{chat_id}|{admin_id}|{text}".encode("utf-8")).hexdigest()
            out.append((record, database._import_id(f"memory:{fp}")))
        else:
            fp = hashlib.sha256(f"{chat_id}|{record['question']}|{record['answer']}".encode("utf-8")).hexdigest()
            out.append((record, database._import_id(f"qa-question:{fp}")))
    return out


def preview(kind: str, records: list[dict[str, Any]], *, chat_id: int, admin_id: int) -> dict[str, Any]:
    pairs = _record_ids(kind, chat_id, admin_id, records)
    ids = [pid for _, pid in pairs]
    if kind == "memory":
        rows = database._fetchall(
            "select telegram_message_id from public.admin_messages where chat_id = %(c)s "
            "and telegram_message_id = any(%(ids)s)", {"c": int(chat_id), "ids": ids})
    else:
        rows = database._fetchall(
            "select question_message_id as telegram_message_id from public.qa_memory where chat_id = %(c)s "
            "and question_message_id = any(%(ids)s)", {"c": int(chat_id), "ids": ids})
    existing = {int(r["telegram_message_id"]) for r in rows}
    duplicates = sum(1 for _, pid in pairs if pid in existing)
    sample = []
    for record, _pid in pairs[:5]:
        if kind == "memory":
            sample.append({"text": record["text"][:200]})
        else:
            sample.append({"question": record["question"][:200], "answer": record["answer"][:200]})
    return {"total": len(records), "new": len(records) - duplicates, "duplicates": duplicates,
            "sample": sample}


def commit(kind: str, records: list[dict[str, Any]], *, chat_id: int, admin_id: int,
           file_name: str, file_sha256: str | None, source_label: str = "web import") -> dict[str, Any]:
    batch = database._fetchone(
        "insert into public.memory_import_batches (kind, file_name, file_sha256, source_label, "
        "total_records, created_by) values (%(k)s, %(f)s, %(h)s, %(s)s, %(t)s, %(a)s) returning id",
        {"k": kind, "f": (file_name or "")[:200], "h": file_sha256, "s": source_label[:80],
         "t": len(records), "a": int(admin_id)})
    batch_id = int(batch["id"])
    if kind == "memory":
        result = database.import_admin_memory(records, chat_id=chat_id, admin_id=admin_id,
                                              source=source_label, batch_id=batch_id)
    else:
        result = database.import_qa_memory(records, chat_id=chat_id, admin_id=admin_id,
                                           source=source_label, batch_id=batch_id)
    database._execute(
        "update public.memory_import_batches set imported = %(i)s, duplicates = %(d)s, failed = %(f)s, "
        "status = 'completed' where id = %(id)s",
        {"i": result["imported"], "d": result["duplicates"], "f": result["failed"], "id": batch_id})
    return {"batch_id": batch_id, **result}
