"""Admin data access. Fixed, parameterised queries only: no free-form SQL is ever
accepted from a request. Every write is validated by the caller and audited by
the blueprint.
"""

from __future__ import annotations

import re
from typing import Any

import database
from utils.text import classify_topic, extract_crypto_terms, extract_keywords, normalize_text
from web import store
from web.timeutil import period_keys

PAGE_SIZE = 25
MEMORY_KINDS = {"memory": ("admin_messages", "message_text", "normalized_text"),
                "qa": ("qa_memory", "question_text", "normalized_question")}
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{2,79}$")
_LIMIT_FIELDS = {
    "daily_request_limit": (0, 10000),
    "monthly_request_limit": (0, 100000),
    "daily_token_limit": (0, 100000000),
    "monthly_token_limit": (0, 1000000000),
    "cooldown_seconds": (0, 600),
    "max_response_tokens": (50, 8000),
}


def _page(value: Any, default: int = 1) -> int:
    try:
        return max(1, min(int(value), 10000))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# overview & usage
# --------------------------------------------------------------------------- #
def overview(chat_id: int | None) -> dict[str, Any]:
    day_key, month_key = period_keys()
    users = database._fetchone(
        "select count(*) filter (where access_status = 'active') as active, "
        "count(*) filter (where access_status = 'suspended') as suspended, "
        "count(*) filter (where access_status = 'blocked') as blocked, count(*) as total "
        "from public.bot_users")
    sessions = store.count_active_sessions()
    today = database._fetchone(
        "select coalesce(sum(requests),0) as requests, coalesce(sum(tokens),0) as tokens "
        "from public.user_usage_periods where period_type = 'day' and period_key = %(d)s", {"d": day_key})
    month = database._fetchone(
        "select coalesce(sum(requests),0) as requests, coalesce(sum(tokens),0) as tokens "
        "from public.user_usage_periods where period_type = 'month' and period_key = %(m)s", {"m": month_key})
    failures = database._fetchone(
        "select count(*) as c from public.ai_request_ledger where status = 'failed' "
        "and created_at > now() - interval '24 hours'")
    conversations = database._fetchone(
        "select count(*) as conversations, coalesce(sum(message_count),0) as messages "
        "from public.web_conversations")
    memory = database._fetchone(
        "select (select count(*) from public.admin_messages) as memory_rows, "
        "(select count(*) from public.qa_memory) as qa_rows")
    return {
        "users": {k: int(v or 0) for k, v in (users or {}).items()},
        "active_sessions": sessions,
        "today": {"requests": int(today["requests"]), "tokens": int(today["tokens"])},
        "month": {"requests": int(month["requests"]), "tokens": int(month["tokens"])},
        "failed_requests_24h": int(failures["c"]),
        "conversations": {k: int(v or 0) for k, v in (conversations or {}).items()},
        "memory": {k: int(v or 0) for k, v in (memory or {}).items()},
        "timezone": "Asia/Dhaka",
    }


def usage_summary(days: int) -> dict[str, Any]:
    days = max(1, min(int(days), 90))
    daily = database._fetchall(
        "select to_char(created_at at time zone 'Asia/Dhaka', 'YYYY-MM-DD') as day, "
        "source, count(*) as requests, coalesce(sum(total_tokens),0) as tokens, "
        "count(*) filter (where status = 'error') as errors "
        "from public.ai_usage_log where created_at > now() - make_interval(days => %(d)s) "
        "group by 1, 2 order by 1 desc, 2", {"d": days})
    ledger = database._fetchall(
        "select status, error_type, count(*) as c from public.ai_request_ledger "
        "where created_at > now() - make_interval(days => %(d)s) group by 1, 2 "
        "order by c desc limit 50", {"d": days})
    top = database._fetchall(
        "select b.telegram_user_id, b.username, b.first_name, "
        "coalesce(sum(p.requests),0) as requests, coalesce(sum(p.tokens),0) as tokens "
        "from public.user_usage_periods p join public.bot_users b on b.telegram_user_id = p.telegram_user_id "
        "where p.period_type = 'day' and p.period_key >= to_char(now() at time zone 'Asia/Dhaka' "
        "- make_interval(days => %(d)s), 'YYYY-MM-DD') group by 1, 2, 3 order by requests desc limit 10",
        {"d": days})
    return {"days": days, "daily": _jsonable(daily), "ledger": _jsonable(ledger),
            "top_users": [{**_jsonable([r])[0], "telegram_user_id": str(r["telegram_user_id"])} for r in top]}


# --------------------------------------------------------------------------- #
# users
# --------------------------------------------------------------------------- #
def list_users(*, query: str = "", status: str = "", page: Any = 1) -> dict[str, Any]:
    page = _page(page)
    clauses = ["true"]
    params: dict[str, Any] = {"limit": PAGE_SIZE, "offset": (page - 1) * PAGE_SIZE}
    if status in ("active", "suspended", "blocked"):
        clauses.append("b.access_status = %(status)s")
        params["status"] = status
    q = (query or "").strip()[:80]
    if q:
        clauses.append("(cast(b.telegram_user_id as text) like %(q)s or coalesce(b.username,'') ilike %(q)s "
                       "or coalesce(b.first_name,'') ilike %(q)s or coalesce(b.last_name,'') ilike %(q)s)")
        params["q"] = f"%{q}%"
    where = " and ".join(clauses)
    rows = database._fetchall(
        f"""
        select b.telegram_user_id, b.username, b.first_name, b.last_name, b.access_status,
               b.last_activity_at, b.first_seen, b.is_target_admin,
               (select count(*) from public.web_sessions s where s.telegram_user_id = b.telegram_user_id
                  and s.revoked_at is null and s.expires_at > now()) as active_sessions
          from public.bot_users b
         where {where}
         order by b.last_activity_at desc nulls last, b.id desc
         limit %(limit)s offset %(offset)s
        """, params)
    total = database._fetchone(f"select count(*) as c from public.bot_users b where {where}", params)
    return {"items": [serialize_user(r) for r in rows], "page": page, "page_size": PAGE_SIZE,
            "total": int((total or {}).get("c") or 0)}


def serialize_user(row: dict[str, Any]) -> dict[str, Any]:
    def iso(v: Any) -> Any:
        return v.isoformat() if hasattr(v, "isoformat") else v
    out = {
        "telegram_user_id": str(row["telegram_user_id"]),
        "username": row.get("username"),
        "first_name": row.get("first_name"),
        "last_name": row.get("last_name"),
        "access_status": row.get("access_status") or "active",
        "last_activity_at": iso(row.get("last_activity_at")),
        "first_seen": iso(row.get("first_seen")),
        "is_target_admin": bool(row.get("is_target_admin")),
        "active_sessions": int(row.get("active_sessions") or 0),
    }
    for key in ("daily_request_limit", "monthly_request_limit", "daily_token_limit", "monthly_token_limit",
                "cooldown_seconds", "max_response_tokens", "model_override", "admin_notes"):
        if key in row:
            out[key] = row.get(key)
    return out


def get_user_detail(telegram_user_id: int) -> dict[str, Any] | None:
    row = database._fetchone(
        "select b.*, (select count(*) from public.web_sessions s where s.telegram_user_id = b.telegram_user_id "
        "and s.revoked_at is null and s.expires_at > now()) as active_sessions "
        "from public.bot_users b where b.telegram_user_id = %(uid)s", {"uid": int(telegram_user_id)})
    if row is None:
        return None
    detail = serialize_user(row)
    day_key, month_key = period_keys()
    usage = database._fetchall(
        "select period_type, period_key, requests, tokens from public.user_usage_periods "
        "where telegram_user_id = %(uid)s and ((period_type='day' and period_key=%(d)s) or "
        "(period_type='month' and period_key=%(m)s))",
        {"uid": int(telegram_user_id), "d": day_key, "m": month_key})
    detail["usage"] = _jsonable(usage)
    detail["conversations"] = int((database._fetchone(
        "select count(*) as c from public.web_conversations where telegram_user_id = %(uid)s",
        {"uid": int(telegram_user_id)}) or {}).get("c") or 0)
    return detail


def clean_user_update(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Validate a per-user update. ``null`` means 'use the default'."""
    clean: dict[str, Any] = {}
    errors: dict[str, str] = {}
    if "access_status" in payload:
        value = str(payload.get("access_status") or "")
        if value not in ("active", "suspended", "blocked"):
            errors["access_status"] = "must be active, suspended or blocked"
        else:
            clean["access_status"] = value
    for key, (low, high) in _LIMIT_FIELDS.items():
        if key not in payload:
            continue
        raw = payload.get(key)
        if raw is None or raw == "":
            clean[key] = None
            continue
        if isinstance(raw, bool):
            errors[key] = "must be a number"
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            errors[key] = "must be a whole number"
            continue
        if not low <= value <= high:
            errors[key] = f"must be between {low} and {high}"
            continue
        clean[key] = value
    if "model_override" in payload:
        raw = payload.get("model_override")
        if raw in (None, ""):
            clean["model_override"] = None
        elif isinstance(raw, str) and _MODEL_RE.match(raw.strip()):
            clean["model_override"] = raw.strip()
        else:
            errors["model_override"] = "invalid model name"
    if "admin_notes" in payload:
        raw = payload.get("admin_notes")
        if raw is None:
            clean["admin_notes"] = None
        elif isinstance(raw, str) and len(raw) <= 500:
            clean["admin_notes"] = raw.replace("\x00", "").strip() or None
        else:
            errors["admin_notes"] = "must be text up to 500 characters"
    return clean, errors


def update_user(telegram_user_id: int, clean: dict[str, Any], actor_id: int) -> dict[str, Any] | None:
    if not clean:
        return get_user_detail(telegram_user_id)
    sets = []
    params: dict[str, Any] = {"uid": int(telegram_user_id), "actor": int(actor_id)}
    for key, value in clean.items():
        sets.append(f"{key} = %({key})s")
        params[key] = value
    if "access_status" in clean:
        sets.append("status_changed_at = now()")
        sets.append("status_changed_by = %(actor)s")
    sets.append("updated_at = now()")
    database._execute(
        f"update public.bot_users set {', '.join(sets)} where telegram_user_id = %(uid)s", params)
    return get_user_detail(telegram_user_id)


# --------------------------------------------------------------------------- #
# memory (admin_messages = Admin Memory, qa_memory = Q&A)
# --------------------------------------------------------------------------- #
def list_memory(kind: str, chat_id: int, *, query: str = "", source: str = "", page: Any = 1) -> dict[str, Any]:
    table, text_col, _norm = MEMORY_KINDS[kind]
    page = _page(page)
    params: dict[str, Any] = {"chat": int(chat_id), "limit": PAGE_SIZE, "offset": (page - 1) * PAGE_SIZE}
    clauses = ["chat_id = %(chat)s"]
    q = (query or "").strip()[:120]
    if q:
        clauses.append(f"{text_col} ilike %(q)s")
        params["q"] = f"%{q}%"
    if source in ("telegram", "import", "web"):
        clauses.append("source_kind = %(src)s")
        params["src"] = source
    where = " and ".join(clauses)
    if kind == "memory":
        cols = ("id, message_text as text, topic, language, message_timestamp as created, source_kind, "
                "import_batch_id, message_type as detail")
    else:
        cols = ("id, question_text as question, admin_answer_text as answer, topic, language, "
                "answer_timestamp as created, source_kind, import_batch_id, usage_count")
    rows = database._fetchall(
        f"select {cols} from public.{table} where {where} order by id desc limit %(limit)s offset %(offset)s",
        params)
    total = database._fetchone(f"select count(*) as c from public.{table} where {where}", params)
    items = []
    for row in rows:
        item = dict(row)
        if hasattr(item.get("created"), "isoformat"):
            item["created"] = item["created"].isoformat()
        items.append(item)
    return {"items": items, "page": page, "page_size": PAGE_SIZE, "total": int((total or {}).get("c") or 0)}


def clean_memory(kind: str, payload: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    errors: dict[str, str] = {}
    out: dict[str, str] = {}
    fields = ("text",) if kind == "memory" else ("question", "answer")
    for field in fields:
        raw = payload.get(field)
        if not isinstance(raw, str):
            errors[field] = "must be text"
            continue
        value = raw.replace("\x00", "").strip()
        if not value:
            errors[field] = "cannot be empty"
        elif len(value) > 2000:
            errors[field] = "must be at most 2000 characters"
        else:
            out[field] = value
    return out, errors


def create_memory(kind: str, fields: dict[str, str], *, chat_id: int, actor_id: int) -> dict[str, Any]:
    from web import imports  # local: keep module graph shallow
    if kind == "memory":
        records = [{"text": fields["text"]}]
    else:
        records = [{"question": fields["question"], "answer": fields["answer"]}]
    result = imports.commit(kind, records, chat_id=chat_id, admin_id=actor_id,
                            file_name="manual entry", file_sha256=None, source_label="web manual")
    return result


def update_memory(kind: str, memory_id: int, fields: dict[str, str], *, chat_id: int) -> bool:
    if kind == "memory":
        text = fields["text"]
        return database._execute(
            "update public.admin_messages set message_text = %(t)s, normalized_text = %(n)s, topic = %(topic)s, "
            "keywords = %(kw)s, crypto_terms = %(cr)s, language = %(lang)s, updated_at = now() "
            "where id = %(id)s and chat_id = %(chat)s",
            {"t": text, "n": normalize_text(text), "topic": classify_topic(text),
             "kw": extract_keywords(text, max_keywords=20), "cr": extract_crypto_terms(text),
             "lang": database._detect_language(text), "id": int(memory_id), "chat": int(chat_id)}) > 0
    question, answer = fields["question"], fields["answer"]
    combined = f"{question} {answer}"
    return database._execute(
        "update public.qa_memory set question_text = %(q)s, normalized_question = %(qn)s, "
        "admin_answer_text = %(a)s, normalized_answer = %(an)s, topic = %(topic)s, keywords = %(kw)s, "
        "crypto_terms = %(cr)s, updated_at = now() where id = %(id)s and chat_id = %(chat)s",
        {"q": question, "qn": normalize_text(question), "a": answer, "an": normalize_text(answer),
         "topic": classify_topic(combined), "kw": extract_keywords(combined, max_keywords=25),
         "cr": extract_crypto_terms(combined), "id": int(memory_id), "chat": int(chat_id)}) > 0


def delete_memory(kind: str, memory_id: int, *, chat_id: int) -> bool:
    table = MEMORY_KINDS[kind][0]
    deleted = database._execute(
        f"delete from public.{table} where id = %(id)s and chat_id = %(chat)s",
        {"id": int(memory_id), "chat": int(chat_id)}) > 0
    if deleted:
        database.bump_memory_generation()
    return deleted


def _dup_clause(kind: str) -> tuple[str, str]:
    table, _text, norm = MEMORY_KINDS[kind]
    return table, norm


def dedupe_preview(kind: str, chat_id: int) -> dict[str, Any]:
    table, norm = _dup_clause(kind)
    groups = database._fetchall(
        f"select {norm} as key, count(*) as copies, array_agg(id order by id) as ids "
        f"from public.{table} where chat_id = %(chat)s and {norm} <> '' "
        f"group by {norm} having count(*) > 1 order by count(*) desc limit 20",
        {"chat": int(chat_id)})
    total = database._fetchone(
        f"select count(*) as c from public.{table} a where a.chat_id = %(chat)s and a.{norm} <> '' "
        f"and exists (select 1 from public.{table} b where b.chat_id = a.chat_id and b.{norm} = a.{norm} "
        f"and b.id < a.id)", {"chat": int(chat_id)})
    return {
        "removable": int((total or {}).get("c") or 0),
        "groups": [{"sample": (g["key"] or "")[:160], "copies": int(g["copies"]),
                    "keep_id": int(g["ids"][0]), "remove_ids": [int(i) for i in g["ids"][1:]][:50]}
                   for g in groups],
        "rule": "The oldest row (lowest id) in each group is kept; newer exact duplicates are removed.",
    }


def dedupe_commit(kind: str, chat_id: int) -> int:
    table, norm = _dup_clause(kind)
    removed = database._execute(
        f"delete from public.{table} a using public.{table} b where a.chat_id = %(chat)s "
        f"and b.chat_id = a.chat_id and b.{norm} = a.{norm} and a.{norm} <> '' and b.id < a.id",
        {"chat": int(chat_id)})
    if removed:
        database.bump_memory_generation()
    return removed


def import_batches(limit: int = 20) -> list[dict[str, Any]]:
    rows = database._fetchall(
        "select id, kind, file_name, source_label, total_records, imported, duplicates, failed, "
        "status, created_by, created_at from public.memory_import_batches order by id desc limit %(l)s",
        {"l": int(max(1, min(limit, 100)))})
    return _jsonable(rows)


# --------------------------------------------------------------------------- #
# conversations (website chats; admin sees metadata, and transcripts only on demand)
# --------------------------------------------------------------------------- #
def list_conversations(*, telegram_user_id: int | None, page: Any = 1) -> dict[str, Any]:
    page = _page(page)
    params: dict[str, Any] = {"limit": PAGE_SIZE, "offset": (page - 1) * PAGE_SIZE,
                              "uid": int(telegram_user_id) if telegram_user_id else None}
    rows = database._fetchall(
        """
        select c.id, c.telegram_user_id, c.title, c.message_count, c.created_at, c.last_message_at,
               b.username, b.first_name
          from public.web_conversations c
          left join public.bot_users b on b.telegram_user_id = c.telegram_user_id
         where (%(uid)s::bigint is null or c.telegram_user_id = %(uid)s)
         order by coalesce(c.last_message_at, c.created_at) desc
         limit %(limit)s offset %(offset)s
        """, params)
    total = database._fetchone(
        "select count(*) as c from public.web_conversations where (%(uid)s::bigint is null or telegram_user_id = %(uid)s)",
        params)
    items = []
    for row in _jsonable(rows):
        row["id"] = str(row["id"])
        row["telegram_user_id"] = str(row["telegram_user_id"])
        items.append(row)
    return {"items": items, "page": page, "page_size": PAGE_SIZE, "total": int((total or {}).get("c") or 0)}


def conversation_transcript(conversation_id: str) -> dict[str, Any] | None:
    head = database._fetchone(
        "select id, telegram_user_id, title, message_count, created_at from public.web_conversations "
        "where id = %(cid)s::uuid", {"cid": conversation_id})
    if head is None:
        return None
    messages = database._fetchall(
        "select id, role, content, status, model, used_memory, created_at from public.web_messages "
        "where conversation_id = %(cid)s::uuid order by id asc limit 300", {"cid": conversation_id})
    out = _jsonable([head])[0]
    out["id"] = str(out["id"])
    out["telegram_user_id"] = str(out["telegram_user_id"])
    out["messages"] = _jsonable(messages)
    return out


def delete_conversation_admin(conversation_id: str) -> bool:
    return database._execute("delete from public.web_conversations where id = %(cid)s::uuid",
                             {"cid": conversation_id}) > 0


# --------------------------------------------------------------------------- #
# audit log
# --------------------------------------------------------------------------- #
def audit_list(page: Any = 1) -> dict[str, Any]:
    page = _page(page)
    rows = database._fetchall(
        "select id, actor_telegram_id, action, target_type, target_id, summary, created_at "
        "from public.admin_audit_log order by id desc limit %(l)s offset %(o)s",
        {"l": PAGE_SIZE, "o": (page - 1) * PAGE_SIZE})
    total = database._fetchone("select count(*) as c from public.admin_audit_log")
    return {"items": _jsonable(rows), "page": page, "page_size": PAGE_SIZE,
            "total": int((total or {}).get("c") or 0)}


# --------------------------------------------------------------------------- #
def _jsonable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        item = {}
        for key, value in row.items():
            if hasattr(value, "isoformat"):
                item[key] = value.isoformat()
            elif isinstance(value, (list, tuple)):
                item[key] = [str(v) if not isinstance(v, (int, float, str, bool)) else v for v in value]
            elif isinstance(value, bytes):
                item[key] = None
            else:
                item[key] = value
        out.append(item)
    return out
