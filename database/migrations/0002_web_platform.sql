-- ============================================================================
--  Migration 0002 - Web platform (chat website + admin dashboard)
-- ----------------------------------------------------------------------------
--  * Additive and idempotent: safe to run on a database that already serves
--    the Telegram bot. Existing rows and columns are never modified or removed.
--  * Applied by:  python scripts/migrate.py --url "$DATABASE_URL"
--                 (or DB_AUTO_MIGRATE=true, or paste into Supabase SQL editor)
--  * Rollback:    database/migrations/0002_web_platform.down.sql drops ONLY the
--                 new web-platform tables. Additive columns are intentionally
--                 kept, because dropping them would destroy user limits/notes.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. bot_users : access control, per-user limits, profile metadata
--    (existing table; new columns are nullable or have safe defaults)
-- ---------------------------------------------------------------------------
alter table public.bot_users
    add column if not exists access_status       text not null default 'active',
    add column if not exists daily_request_limit   integer,
    add column if not exists monthly_request_limit integer,
    add column if not exists daily_token_limit     bigint,
    add column if not exists monthly_token_limit   bigint,
    add column if not exists max_response_tokens   integer,
    add column if not exists cooldown_seconds      integer,
    add column if not exists model_override        text,
    add column if not exists admin_notes           text,
    add column if not exists photo_url             text,
    add column if not exists last_activity_at      timestamptz,
    add column if not exists last_ai_request_at    timestamptz,
    add column if not exists status_changed_at     timestamptz,
    add column if not exists status_changed_by     bigint;

do $$
begin
    if not exists (select 1 from pg_constraint where conname = 'ck_bot_users_access_status') then
        alter table public.bot_users add constraint ck_bot_users_access_status
            check (access_status in ('active', 'suspended', 'blocked'));
    end if;
    if not exists (select 1 from pg_constraint where conname = 'ck_bot_users_limits_nonneg') then
        alter table public.bot_users add constraint ck_bot_users_limits_nonneg
            check (coalesce(daily_request_limit, 0) >= 0
               and coalesce(monthly_request_limit, 0) >= 0
               and coalesce(daily_token_limit, 0) >= 0
               and coalesce(monthly_token_limit, 0) >= 0
               and coalesce(max_response_tokens, 1) >= 1
               and coalesce(cooldown_seconds, 0) >= 0);
    end if;
end $$;

create index if not exists idx_bot_users_access_status on public.bot_users (access_status);

-- ---------------------------------------------------------------------------
-- 2. ai_usage_log : which surface made the call (telegram | web)
-- ---------------------------------------------------------------------------
alter table public.ai_usage_log add column if not exists source text not null default 'telegram';
create index if not exists idx_ai_usage_source_created on public.ai_usage_log (source, created_at desc);

-- ---------------------------------------------------------------------------
-- 3. admin_messages / qa_memory : provenance (live | import | manual)
--    so the dashboard can filter by source and show import history.
-- ---------------------------------------------------------------------------
alter table public.admin_messages
    add column if not exists source_kind     text not null default 'telegram',
    add column if not exists import_batch_id bigint,
    add column if not exists updated_at      timestamptz;

alter table public.qa_memory
    add column if not exists source_kind     text not null default 'telegram',
    add column if not exists import_batch_id bigint,
    add column if not exists updated_at      timestamptz;

create index if not exists idx_admin_messages_source on public.admin_messages (source_kind, message_timestamp desc);
create index if not exists idx_admin_messages_normalized on public.admin_messages (chat_id, normalized_text);
create index if not exists idx_qa_memory_source on public.qa_memory (source_kind, answer_timestamp desc);
create index if not exists idx_qa_memory_normalized_question on public.qa_memory (chat_id, normalized_question);

-- ---------------------------------------------------------------------------
-- 4. memory_import_batches : import history (who, when, what, outcome)
-- ---------------------------------------------------------------------------
create table if not exists public.memory_import_batches (
    id              bigserial primary key,
    kind            text not null check (kind in ('memory', 'qa')),
    file_name       text,
    file_sha256     text,
    source_label    text,
    total_records   integer not null default 0,
    imported        integer not null default 0,
    duplicates      integer not null default 0,
    failed          integer not null default 0,
    verified_count  integer,
    verified_total  integer,
    status          text not null default 'completed' check (status in ('completed', 'failed')),
    created_by      bigint not null,
    created_at      timestamptz not null default now()
);
create index if not exists idx_import_batches_created on public.memory_import_batches (created_at desc);

-- ---------------------------------------------------------------------------
-- 5. ai_request_ledger : one row per web AI request (no message content).
--    request_uuid is client-generated and UNIQUE: it makes retries idempotent
--    so a duplicate request can never be charged twice.
-- ---------------------------------------------------------------------------
create table if not exists public.ai_request_ledger (
    id                bigserial primary key,
    request_uuid      text not null unique check (char_length(request_uuid) between 8 and 64),
    telegram_user_id  bigint not null,
    source            text not null default 'web',
    conversation_id   uuid,
    status            text not null default 'reserved'
                      check (status in ('reserved', 'succeeded', 'failed')),
    model             text,
    prompt_tokens     integer not null default 0,
    completion_tokens integer not null default 0,
    total_tokens      integer not null default 0,
    latency_ms        integer not null default 0,
    error_type        text,
    day_key           text not null,
    month_key         text not null,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now()
);
create index if not exists idx_ai_request_ledger_user on public.ai_request_ledger (telegram_user_id, created_at desc);
create index if not exists idx_ai_request_ledger_status on public.ai_request_ledger (status, created_at);
create index if not exists idx_ai_request_ledger_day on public.ai_request_ledger (day_key);

-- ---------------------------------------------------------------------------
-- 6. user_usage_periods : atomic per-user counters (day / month, Asia/Dhaka)
-- ---------------------------------------------------------------------------
create table if not exists public.user_usage_periods (
    telegram_user_id  bigint not null,
    period_type       text not null check (period_type in ('day', 'month')),
    period_key        text not null,
    requests          integer not null default 0 check (requests >= 0),
    tokens            bigint not null default 0 check (tokens >= 0),
    updated_at        timestamptz not null default now(),
    primary key (telegram_user_id, period_type, period_key)
);
create index if not exists idx_user_usage_periods_key on public.user_usage_periods (period_type, period_key);

-- ---------------------------------------------------------------------------
-- 7. web_sessions : server-side sessions (only SHA-256 digests are stored)
-- ---------------------------------------------------------------------------
create table if not exists public.web_sessions (
    id                     bigserial primary key,
    session_hash           text not null unique check (char_length(session_hash) = 64),
    csrf_hash              text not null check (char_length(csrf_hash) = 64),
    telegram_user_id       bigint not null,
    role                   text not null check (role in ('member', 'admin')),
    ip_hash                text,
    user_agent_hash        text,
    created_at             timestamptz not null default now(),
    last_seen_at           timestamptz not null default now(),
    expires_at             timestamptz not null,
    membership_checked_at  timestamptz,
    revoked_at             timestamptz,
    revoked_reason         text
);
create index if not exists idx_web_sessions_user on public.web_sessions (telegram_user_id);
create index if not exists idx_web_sessions_expiry on public.web_sessions (expires_at);

-- ---------------------------------------------------------------------------
-- 8. web_login_replays : each accepted Telegram login payload is single-use
-- ---------------------------------------------------------------------------
create table if not exists public.web_login_replays (
    payload_hash      text primary key check (char_length(payload_hash) = 64),
    telegram_user_id  bigint not null,
    auth_date         bigint not null,
    expires_at        timestamptz not null,
    created_at        timestamptz not null default now()
);
create index if not exists idx_web_login_replays_expiry on public.web_login_replays (expires_at);

-- ---------------------------------------------------------------------------
-- 9. web_conversations / web_messages : private chat history of website users.
--    Kept separate from admin_messages and qa_memory on purpose: private chats
--    are NEVER turned into Admin Memory.
-- ---------------------------------------------------------------------------
create table if not exists public.web_conversations (
    id                uuid primary key default gen_random_uuid(),
    telegram_user_id  bigint not null,
    title             text not null default 'New chat' check (char_length(title) <= 120),
    message_count     integer not null default 0,
    last_message_at   timestamptz,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now()
);
create index if not exists idx_web_conversations_user on public.web_conversations (telegram_user_id, updated_at desc);
create index if not exists idx_web_conversations_created on public.web_conversations (created_at);

create table if not exists public.web_messages (
    id                bigserial primary key,
    conversation_id   uuid not null references public.web_conversations(id) on delete cascade,
    telegram_user_id  bigint not null,
    role              text not null check (role in ('user', 'assistant')),
    content           text not null check (char_length(content) <= 20000),
    status            text not null default 'complete'
                      check (status in ('complete', 'error', 'fallback')),
    model             text,
    used_memory       boolean not null default false,
    total_tokens      integer not null default 0,
    request_uuid      text,
    created_at        timestamptz not null default now()
);
create index if not exists idx_web_messages_conversation on public.web_messages (conversation_id, id);
create index if not exists idx_web_messages_created on public.web_messages (created_at);

-- ---------------------------------------------------------------------------
-- 10. admin_audit_log : every administrative change (who / what / when)
-- ---------------------------------------------------------------------------
create table if not exists public.admin_audit_log (
    id                bigserial primary key,
    actor_telegram_id bigint not null,
    action            text not null,
    target_type       text,
    target_id         text,
    summary           text not null default '',
    details           jsonb not null default '{}'::jsonb,
    created_at        timestamptz not null default now()
);
create index if not exists idx_admin_audit_created on public.admin_audit_log (created_at desc);
create index if not exists idx_admin_audit_action on public.admin_audit_log (action, created_at desc);

-- ---------------------------------------------------------------------------
-- 11. Row Level Security: browser-facing Supabase APIs must never read these.
--     No permissive policies are created; only the server connection (which
--     owns the tables) can read or write. Same approach as schema.sql.
-- ---------------------------------------------------------------------------
do $$
declare
    t text;
begin
    foreach t in array array[
        'memory_import_batches', 'ai_request_ledger', 'user_usage_periods',
        'web_sessions', 'web_login_replays', 'web_conversations', 'web_messages',
        'admin_audit_log'
    ]
    loop
        execute format('alter table public.%I enable row level security', t);
    end loop;
end $$;

-- ---------------------------------------------------------------------------
-- 12. default runtime settings (existing bot_settings table, safe to re-run)
-- ---------------------------------------------------------------------------
insert into public.bot_settings (key, value, value_type, description) values
    ('web_chat_enabled', 'true', 'bool', 'Website AI chat on/off (admin switch)')
on conflict (key) do nothing;
