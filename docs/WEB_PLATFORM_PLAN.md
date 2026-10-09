# Website Platform — Plan, Status & File List

এই file-এ থাকে: উদ্দেশ্য, মূল সিদ্ধান্ত, প্রতিটি অংশের বর্তমান অবস্থা ও প্রমাণ, খোলা বিষয়, এবং পরিবর্তিত/নতুন file-এর পূর্ণ তালিকা। Setup ও deploy-এর ধাপ আছে [WEB_PLATFORM.md](WEB_PLATFORM.md)-এ।

## 1. উদ্দেশ্য

বিদ্যমান Telegram AI bot-কে (Flask/gunicorn, Groq, Supabase PostgreSQL) নষ্ট না করে একটি web platform-এ উন্নীত করা:

- Dark, responsive AI chat (`/`) — history, rename/delete, copy/regenerate, safe markdown, quota
- Telegram Login (`/login`) — server-side verification, group membership, fail-closed
- Per-user limits ও status, backend-এ enforcement, Asia/Dhaka period
- `/admin_panel` ও `/api/admin/*` — শুধু `ADMIN_ID`, প্রতিটি API আলাদা যাচাই
- Website ও bot একই AI service ব্যবহার করে; website data আলাদা ও memory-তে যায় না
- Render Free ও Flask/gunicorn stack অপরিবর্তিত; কোনো অতিরিক্ত frontend framework নেই

## 2. মূল সিদ্ধান্ত

| বিষয় | সিদ্ধান্ত | কারণ |
|---|---|---|
| Login | Telegram Login Widget **redirect mode** (`data-auth-url`) → `GET /api/auth/telegram/callback` | Widget-এর `data-onauth` eval ব্যবহার করে, যা strict CSP ব্লক করে |
| Login CSRF | `ig_login_nonce` HttpOnly cookie (১০ মিনিট) ও query nonce মেলানো | অন্য browser থেকে বানানো link কাজ করবে না |
| Replay | `auth_date + max_age + 120s` পর্যন্ত payload digest store | একই payload দুইবার নয় |
| Membership | Bot API `getChatMember`, synchronous; ব্যর্থ হলে fail-closed | নিরাপত্তা |
| Session | Token শুধু hash; absolute expiry; revocation তাৎক্ষণিক | DB leak-এ token উদ্ধার হবে না |
| CSRF | Session-সাথে সই করা token (`WEB_SECRET_KEY`, না থাকলে BOT_TOKEN থেকে) | |
| Streaming | নেই। শুধু typing indicator | Groq response সম্পূর্ণ হলে দেখানো সহজ ও নির্ভরযোগ্য |
| Quota | Request limit `None` = unlimited, `0` = বন্ধ; token `0` = unlimited; reserve আগে, charge পরে | AI call-এর আগে backend enforcement |
| Quota charge | Provider ব্যর্থ বা fallback উত্তরে charge নেই | Retry-তে double charge নয় |
| Retry dedupe | একই request-id-র replay → আগের উত্তর, আবার charge নয় | |
| Website cache | `use_cache=False, store_cache=False` | Private question shared cache-এ গেলে group-এ ফাঁস হতো |
| Admin import | Preview token ~১০ মিনিট, সর্বোচ্চ ২০টি, commit-এ expected count মিলতে হবে | Preview ছাড়া লেখা নয় |
| Migration | Versioned, checksum, এক transaction প্রতি file; `scripts/migrate.py` | `DB_AUTO_MIGRATE` পুরনো schema applier, versioned নয় |
| Rollback | শুধু website-এর নতুন table মোছে | Bot data অক্ষত |
| Deploy | `render.yaml` Blueprint | সব env এক জায়গায়, generated secret সহ |

## 3. অবস্থা ও প্রমাণ

যা চালানো হয়েছে, তার ফলাফল এখানে; অন্য কিছু দাবি করা হয়নি।

| অংশ | অবস্থা | প্রমাণ |
|---|---|---|
| Python test suite | ✅ **423 passed**, ০ failed | `TEST_DATABASE_URL=… python -m pytest -o addopts="" -q tests` |
| Markdown renderer (Node) | ✅ **9 passed** | `node --test tests/js/` |
| Static checks | ✅ compile ও pyflakes পরিষ্কার | `compileall`, `pyflakes services.py web/*.py config.py app.py` |
| Blueprint (`render.yaml`) | ✅ YAML parse, কোনো duplicate key নেই, ৩৯টি env var; ৭টি `sync: false`, ২টি generated | PyYAML দিয়ে যাচাই |
| Dependency name ও call signature | ✅ ৫৯টি module attribute ও ৬৯টি call-site signature-এর সাথে মেলে | AST + `inspect.signature` যাচাই |
| Browser check (স্থানীয় harness, Telegram API-র stub) | ✅ **56/56 passed** | Desktop/mobile login, chat, copy, regenerate, rename, delete, markdown safety, admin tabs, settings, import preview, 403, logout, console errors |
| Login callback test | ✅ valid, missing/mismatched nonce, login CSRF, tamper, unknown param, replay, non-member, error param no-echo | `tests/test_web_platform.py::TestTelegramRedirectLogin` |
| Retention | ✅ পুরনো chat মোছে, নতুন থাকে | `TestWebRetention` |
| Retry dedupe | ✅ ব্যর্থ প্রশ্নের retry একবারই সংরক্ষিত হয় | `test_retry_of_a_failed_question_stores_it_once` |

### এই session-এ ঠিক করা বাগ (test/browser-এ ধরা পড়া)

1. Login-এর `data-onauth` ব্যবহার → CSP eval ব্লক → redirect mode-এ রূপান্তর।
2. Regenerate বোতাম শুধু reload-এর পরে দেখাত (render-এর সময় `busy` ছিল) → এখন সব সময় দেখায়, busy হলে disabled।
3. Admin settings-এর save ফলাফল re-render-এ মুছে যেত → এখন একবার দেখায়।
4. Retry-তে ব্যর্থ প্রশ্ন দুইবার সংরক্ষিত হতো → এখন একবার।
5. `WEB_CHAT_RETENTION_DAYS` ও session purge কোথাও কল হতো না → এখন `services.run_maintenance`-এ যুক্ত।

## 4. খোলা বিষয় (সৎভাবে)

- **আসল Telegram login end-to-end test হয়নি।** Browser test স্থানীয় Telegram API stub দিয়ে। Deploy-এর পর নিজের account দিয়ে যাচাই দরকার।
- **Production migration এখনো চালানো হয়নি।** `python scripts/migrate.py` (Supabase, backup-সহ) আপনার দায়িত্ব।
- **Render-এ Blueprint এখনো import করা হয়নি।** YAML যাচাই হয়েছে, Render dashboard-এ import ও deploy হয়নি।
- **Git history-তে exposed secret।** আগে commit হওয়া `.env` ও `_.env`-এর BOT_TOKEN, GROQ_API_KEY, DATABASE_URL পুনরায় তৈরি করতে হবে। বর্তমান `.env` `.gitignore`-এ আছে এবং git-এ নেই।
- Streaming নেই (ইচ্ছাকৃত)।
- Import preview token ও login throttle process-এর ভেতরে; একাধিক instance-এ ভাগ হবে না।
- `WEB_SECRET_KEY` না দিলে BOT_TOKEN থেকে key তৈরি হয় — Render-এ generated key ব্যবহার করুন।
- Rollback migration website data ও admin audit log মুছে দেয়।

## 5. পরিবর্তিত ও নতুন file

**পরিবর্তিত (modified, 15টি file):**

- `.env.example` — website, login, quota env যোগ
- `ai/prompts.py` — admin-এর answer style/language/extra instruction (bot ও web-এ একই)
- `ai/responder.py` — `store_cache` parameter; web answer cache-এ যায় না
- `app.py` — ProxyFix, web template/static, `register_web`
- `config.py` — web, login, membership, quota setting ও validation
- `database.py` — `log_ai_usage` constant SQL; import SQL constant
- `render.yaml` — Blueprint: সব env, generated secret, health check
- `README.md` — Website অংশ ও docs-এর লিংক
- `DEPLOY_A_TO_Z.md` — নতুন migration ও docs-এর নির্দেশ
- `services.py` — maintenance-এ web retention/session purge
- `tests/conftest.py`, `tests/fakes.py` — dummy env, `telegram_stub`, `get_chat_member`
- `tests/test_flask_app.py`, `tests/test_handlers.py`, `tests/test_permissions.py` — বর্তমান policy অনুযায়ী আপডেট

**নতুন (new, 38টি file):**

- Migration: `database/migrations/0002_web_platform.sql`, `database/migrations/0002_web_platform.down.sql`, `database_migrations.py`, `scripts/migrate.py`
- Docs: `docs/WEB_PLATFORM.md`, `docs/WEB_PLATFORM_PLAN.md`, `docs/supabase_paste_0002_web_platform.sql` (Supabase SQL editor-এ পেস্ট করার জন্য)
- Web backend: `web/__init__.py`, `web/admin_store.py`, `web/auth.py`, `web/blueprint.py`, `web/chat_service.py`, `web/imports.py`, `web/logbuffer.py`, `web/quota.py`, `web/security.py`, `web/settings_service.py`, `web/store.py`, `web/telegram_api.py`, `web/telegram_auth.py`, `web/timeutil.py`
- Templates: `web/templates/base.html`, `web/templates/login.html`, `web/templates/chat.html`, `web/templates/admin.html`, `web/templates/forbidden.html`, `web/templates/error.html`
- Static: `web/static/css/app.css`, `web/static/js/common.js`, `web/static/js/login.js`, `web/static/js/chat.js`, `web/static/js/markdown.js`, `web/static/js/admin.js`
- Tests: `tests/test_web_platform.py`, `tests/test_web_units.py`, `tests/test_web_contract.py`, `tests/web_fakes.py`, `tests/js/markdown.test.js`

**অপরিবর্তিত (গুরুত্বপূর্ণ):** `requirements.txt` (কোনো নতুন dependency নেই), `Procfile`, `.python-version`, `database/schema.sql`, Telegram handler, webhook, Groq provider, model config, Admin Memory ও Q&A retrieval।

## 6. পরবর্তী ধাপ (আপনার দিক থেকে)

1. Git history-র exposed token/key/password পুনরায় তৈরি করুন।
2. Supabase-এর backup নিন, তারপর `docs/supabase_paste_0002_web_platform.sql` পুরোটা SQL editor-এ পেস্ট করে Run দিন (অথবা লোকালে `python scripts/migrate.py`)।
3. Render → New → Blueprint → repo select → secret মানগুলো বসান।
4. BotFather `/setdomain` ঠিক Render domain-এ দিন।
5. নিজের account দিয়ে `/login` → `/admin_panel` যাচাই করুন।
