-- ============================================================================
--  ROLLBACK for migration 0002 (web platform)
-- ----------------------------------------------------------------------------
--  Drops ONLY the tables that exist solely for the website + dashboard.
--  It does NOT drop additive columns on bot_users / admin_messages / qa_memory /
--  ai_usage_log, because they may hold admin-entered limits, notes and import
--  provenance. Telegram bot behaviour is unaffected by this rollback.
--  Run only after taking a backup (Admin panel -> System -> Export backup).
-- ============================================================================
drop table if exists public.web_messages;
drop table if exists public.web_conversations;
drop table if exists public.web_login_replays;
drop table if exists public.web_sessions;
drop table if exists public.user_usage_periods;
drop table if exists public.ai_request_ledger;
drop table if exists public.memory_import_batches;
drop table if exists public.admin_audit_log;
delete from public.bot_settings where key = 'web_chat_enabled';
