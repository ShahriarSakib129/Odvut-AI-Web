# INFO GROUP AI BOT — Website Platform: Setup & Deploy Guide

এই guide-এ আছে: বর্তমান কী আছে, local-এ কীভাবে চালাবেন, BotFather ও Supabase কীভাবে ঠিক করবেন, Render-এ Blueprint দিয়ে deploy, প্রথম admin login, আর সমস্যার সমাধান।

> **প্রথম কাজ:** Git history-তে আগে `.env` ও `_.env` ফাইল commit হয়েছিল, যাতে `BOT_TOKEN`, `GROQ_API_KEY` ও `DATABASE_URL` ছিল। এগুলোকে exposed ধরে নিন। Deploy-এর আগে BotFather-এ `/revoke` দিয়ে নতুন bot token নিন, Groq-এ নতুন key বানান, Supabase-এর database password বদলান।

---

## 1. বর্তমান কী আছে

| এলাকা | কী করে |
|---|---|
| **Website `/`** | AI chat: conversation list, নতুন chat, rename, delete, copy, regenerate, markdown (safe rendering), quota panel, Telegram নাম ও avatar |
| **Login `/login`** | Telegram Login Widget, redirect mode। Server-side HMAC verification, replay ও expiry check, group membership check (Bot API, fail-closed), login throttle |
| **Quota** | প্রতি user-এর daily/monthly request, token, cooldown, model override, status (Active / Suspended / Blocked)। সব enforcement backend-এ, AI provider call-এর আগে। Period হিসাব Asia/Dhaka-তে |
| **Admin `/admin_panel`** | Overview, Users, Settings (validated, restart-required জানায়), Memory & Q&A (CRUD, import preview, dedupe), Conversations, Usage & audit, Diagnostics |
| **Admin API `/api/admin/*`** | প্রতিটি request-এ আলাদাভাবে `ADMIN_ID` যাচাই হয়। SQL চালানোর বা shell-এর সুযোগ নেই |
| **Bot** | আগের Telegram bot অপরিবর্তিত। Website ও bot একই AI service (`ai/responder.py`) ব্যবহার করে |

**Website ও Telegram-এর data আলাদা:** website-এর conversation ও message `web_conversations` / `web_messages`-এ থাকে। এগুলো কখনো `admin_messages` বা `qa_memory`-তে copy হয় না, তাই private chat কখনো memory হয় না।

**AI streaming নেই:** Groq-এর response সম্পূর্ণ হলে উত্তর দেখায়। "Stop" button নেই।

---

## 2. প্রয়োজনীয় জিনিস

- Python 3.11 (`.python-version`-এ `3.11`)
- PostgreSQL: Supabase (production) বা local PostgreSQL 14+ (test)। `pg_trgm` extension লাগে
- Telegram bot (BotFather), একটি Telegram group
- Groq API key
- GitHub account ও Render account (Free plan চলবে)

---

## 3. BotFather ও Telegram group

1. **Bot token:** `@BotFather` → `/newbot` (নতুন হলে) বা `/revoke` (পুরনো token বাতিল করে নতুন নিতে)। নতুন token হলো `BOT_TOKEN`।
2. **Website login domain:** `@BotFather` → `/setdomain` → bot select করুন → আপনার domain দিন, যেমন `info-group-ai-bot.onrender.com`। Telegram Login Widget এই domain allow না থাকলে কাজ করবে না। Custom domain ব্যবহার করলে সেটাও এখানে দিন।
3. **Group:** bot-কে group-এ member হিসেবে যোগ করুন। Bot না থাকলে Telegram membership check বিফল হবে এবং login বন্ধ থাকবে (এটা fail-closed নিরাপত্তা নিয়ম)।
4. **Group ID:** group-এ `/whoami` লিখুন। উত্তরে যে ID আসে (সাধারণত `-100...` দিয়ে শুরু) সেটা `GROUP_ID`।
5. **আপনার নিজের ID:** `/whoami` বা `@userinfobot`। এটা `ADMIN_ID`। Website admin ও bot admin দুটোই এই মান থেকে নির্ধারিত হয়।

---

## 4. Supabase database ও migration

### 4.1 Backup (বাধ্যতামূলক)
Supabase dashboard → Database → Backups থেকে একটি backup নিন। Migration additive, কিন্তু production data-এর জন্য backup নেওয়াই নিয়ম।

### 4.2 Connection string
Supabase → Connect → **Session pooler** connection string নিন। এটা `DATABASE_URL`। Password-এর বিশেষ অক্ষর থাকলে URL-encode করুন।

### 4.3 Migration চালান (একবার, deploy-এর আগে)
লোকালে:

```bash
cd Odvut-AI
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export DATABASE_URL="postgresql://...supabase connection string..."
python scripts/migrate.py --status     # কোন migration বাকি দেখুন
python scripts/migrate.py              # 0002_web_platform apply হবে
python scripts/migrate.py --status     # এখন applied দেখাবে
```

`scripts/migrate.py` প্রতিটি migration এক transaction-এ চালায়, checksum রাখে, আর যে migration একবার চলেছে সেটা দ্বিতীয়বার চালায় না।

**Migration 0002 কী করে:**
- নতুন column যোগ করে: `bot_users` (access status, quota, cooldown, model override, profile), `ai_usage_log.source`, `admin_messages` ও `qa_memory` (source_kind, import_batch_id, updated_at)
- নতুন table তৈরি করে: `web_conversations`, `web_messages`, `web_sessions`, `web_login_replays`, `user_usage_periods`, `ai_request_ledger`, `memory_import_batches`, `admin_audit_log`
- ৮টি নতুন table-এ RLS চালু করে। কোনো permissive policy নেই, তাই Supabase-এর public API দিয়ে এগুলো পড়া যায় না। শুধু server connection পড়তে পারে
- কোনো existing row, column বা table মোছে না

### 4.3a Supabase SQL editor দিয়ে চালানো (কমান্ড লাইন ছাড়াই)

Supabase Dashboard → **SQL Editor** → New query → `docs/supabase_paste_0002_web_platform.sql` ফাইলের **পুরো** ভেতরটা পেস্ট করে **Run** দিন।

- ফাইলটি একটি transaction: কোনো ধাপ ব্যর্থ হলে সব rollback হয়, অর্ধেক অবস্থায় থাকে না।
- আবার চালালেও সমস্যা নেই (idempotent)।
- এটি বিদ্যমান data বা column মোছে না।
- শর্ত: মূল schema আগে থেকে থাকতে হবে (`database/schema.sql`)। README-এর "SQL schema চালান" ধাপ যদি আগে করা থাকে, তাহলে এটি ঠিক আছে। একেবারে খালি নতুন project হলে আগে `database/schema.sql` পুরোটা পেস্ট করে চালান।
- চালানোর পর `python scripts/migrate.py --status` (লোকালে `DATABASE_URL` দিয়ে) `0002` applied দেখাবে।

এই ফাইলটি এবং `database/migrations/0002_web_platform.sql` একই schema তৈরি করে। ফাইলটি একটি খালি scratch database-এ `schema.sql` চালিয়ে দুবার চালানো হয়েছে, এবং ফলাফল `scripts/migrate.py` দিয়ে তৈরি schema-র সাথে হুবহু মিলেছে।

### 4.4 Rollback (শুধু প্রয়োজনে)
```bash
python scripts/migrate.py --rollback 0002
```
⚠️ এটা website-এর সব data মুছবে: conversation, message, session, usage ledger, import batch এবং **admin audit log**। পুরনো bot-এর data (admin memory, Q&A, bot users) অক্ষত থাকে। Rollback-এর আগে audit log-এর export নিন।

---

## 5. Environment variables

Render-এ `render.yaml` (Blueprint) এগুলো তৈরি করে। `sync: false` দাগানো মানগুলো Render আপনার কাছে চাইবে। লোকাল জন্য `.env.example`-এর সব key আছে।

### 5.1 Secrets ও identity (Render আপনার কাছে চাইবে)

| Key | মান | কোথা থেকে |
|---|---|---|
| `BOT_TOKEN` | Telegram bot token | BotFather |
| `GROQ_API_KEY` | Groq key | console.groq.com |
| `DATABASE_URL` | Supabase Session pooler URL | Supabase → Connect |
| `PUBLIC_URL` | `https://<service>.onrender.com` (শেষে `/` নয়) | Render service URL |
| `GROUP_ID` | Group ID, `-100...` | `/whoami` in group |
| `ADMIN_ID` | আপনার Telegram numeric ID | `/whoami` |
| `TARGET_ADMIN_ID` | ঐচ্ছিক। যে admin-এর group বার্তা memory-তে যাবে | `/whoami` |

### 5.2 Generated secrets (Render নিজে বানায়)

| Key | কাজ |
|---|---|
| `WEBHOOK_SECRET` | Telegram webhook যাচাই |
| `WEB_SECRET_KEY` | CSRF token সই করা। খালি থাকলে `BOT_TOKEN` থেকে derive হয়, তবে আলাদা key ভালো |

লোকালে নিজে বানাতে: `python -c "import secrets; print(secrets.token_urlsafe(48))"`

### 5.3 Website

| Key | Default | অর্থ |
|---|---|---|
| `WEB_ENABLED` | `true` | `false` করলে website বন্ধ, bot চলবে |
| `WEB_COOKIE_SECURE` | HTTPS হলে `true` | Render-এ অবশ্যই `true` |
| `WEB_SESSION_HOURS` | `12` | Session মেয়াদ (1–168 ঘণ্টা) |
| `WEB_MAX_CONCURRENT_AI` | `3` | একসাথে কতগুলো AI answer (1–20) |
| `WEB_HISTORY_MESSAGES` | `6` | Model-কে আগের কতগুলো message দেওয়া হবে (0–20) |
| `WEB_MAX_MESSAGE_CHARS` | `2000` | এক message-এর সর্বোচ্চ দৈর্ঘ্য |
| `WEB_CHAT_RETENTION_DAYS` | `30` | এর চেয়ে পুরনো website chat maintenance-এ মুছে যায় |

### 5.4 Telegram login

| Key | Default | অর্থ |
|---|---|---|
| `TELEGRAM_AUTH_MAX_AGE_SECONDS` | `300` | Login payload কতক্ষণ বৈধ (60–86400) |
| `MEMBERSHIP_RECHECK_SECONDS` | `300` | Group membership কতক্ষণ পর আবার Bot API-তে যাচাই (30–86400) |
| `LOGIN_RATE_LIMIT_PER_WINDOW` | `10` | ৫ মিনিটে IP ও user প্রতি সর্বোচ্চ login চেষ্টা |

### 5.5 Quota (নতুন user-এর default; admin প্রতি user আলাদা করতে পারে)

| Key | Default | অর্থ |
|---|---|---|
| `QUOTA_DEFAULT_DAILY_REQUESTS` | `30` | দৈনিক request (0 = বন্ধ) |
| `QUOTA_DEFAULT_MONTHLY_REQUESTS` | `600` | মাসিক request |
| `QUOTA_DEFAULT_COOLDOWN_SECONDS` | `0` | দুটি request-এর মধ্যে ন্যূনতম বিরতি |
| `QUOTA_DEFAULT_DAILY_TOKENS` / `QUOTA_DEFAULT_MONTHLY_TOKENS` | `0` | Token limit; `0` = unlimited |
| `QUOTA_ADMIN_DAILY_REQUESTS` | `300` | শুধু `ADMIN_ID`-এর জন্য |
| `QUOTA_ADMIN_MONTHLY_REQUESTS` | `6000` | শুধু `ADMIN_ID`-এর জন্য |
| `QUOTA_ADMIN_DAILY_TOKENS` / `QUOTA_ADMIN_MONTHLY_TOKENS` | `0` | `0` = unlimited |

Quota নিয়ম: token limit আগে যাচাই হয়, উত্তরের পরে charge হয়। একটি উত্তর limit-এর সীমা সামান্য ছাড়াতে পারে (সর্বোচ্চ একটি response)। Provider ব্যর্থ হলে বা fallback উত্তর দিলে quota charge হয় না।

### 5.6 Runtime

| Key | মান | নোট |
|---|---|---|
| `ENVIRONMENT` | `production` | |
| `DB_AUTO_MIGRATE` | `false` | পুরনো schema applier। **Versioned migration এটা চালায় না** — migration সবসময় `scripts/migrate.py` দিয়ে হাতে চালান |
| `SET_WEBHOOK_ON_STARTUP` | `true` | Startup-এ webhook নিবন্ধন |
| `WEBHOOK_REQUIRE_SECRET` | `true` | Webhook secret ছাড়া request গ্রহণ নয় |

---

## 6. লোকালে চালানো

```bash
cp .env.example .env          # তারপর .env খুলে মানগুলো বসান (কখনো commit করবেন না)
# বিশেষ করে: BOT_TOKEN, GROQ_API_KEY, DATABASE_URL, ADMIN_ID, GROUP_ID
# লোকালে HTTP ব্যবহার করলে: WEB_COOKIE_SECURE=false, PUBLIC_URL=http://127.0.0.1:5000
#                          AUTOSTART_BOT=false (bot polling/webhook ছাড়া শুধু website চালাতে)
python app.py                 # http://127.0.0.1:5000
```

`.env` ফাইল `.gitignore`-এ আছে। Commit হওয়ার আগে `git status` দেখে নিন।

---

## 7. Render-এ Deploy (Blueprint)

`render.yaml` একটি Blueprint। এতে web service, build ও start command, health check (`/health`), এবং সব env var (secret ও generated সহ) সংজ্ঞায়িত আছে।

### 7.1 GitHub-এ push
```bash
git add -A
git status          # .env বা কোনো secret file যেন তালিকায় না থাকে
git commit -m "Website platform"
git push origin main
```

### 7.2 Blueprint তৈরি
1. Render dashboard → **New** → **Blueprint**
2. GitHub repository select করুন → **Connect** → **Apply**
3. Render যে মানগুলো চায় (`sync: false`) সেগুলো বসান:
   `BOT_TOKEN`, `GROQ_API_KEY`, `DATABASE_URL`, `PUBLIC_URL`, `GROUP_ID`, `ADMIN_ID`, `TARGET_ADMIN_ID` (ঐচ্ছিক)
4. `WEBHOOK_SECRET` ও `WEB_SECRET_KEY` Render নিজে বানাবে।
5. **Create** চাপুন।

### 7.3 `PUBLIC_URL` সঠিক করা
Render দেওয়া URL (যেমন `https://info-group-ai-bot.onrender.com`) কপি করে `PUBLIC_URL`-এ বসান। এটা Telegram webhook ও login callback-এর ভিত্তি। URL বদলালে BotFather `/setdomain`-ও আপডেট করতে হবে।

### 7.4 প্রথম deploy যাচাই
1. Build log-এ `pip install` সফল দেখুন।
2. `https://<service>.onrender.com/health` খুলুন। JSON দেখাবে।
3. Log-এ webhook নিবন্ধনের লাইন দেখুন। না থাকলে লোকালে `python scripts/set_webhook.py --info` চালিয়ে অবস্থা দেখুন, এবং `--set` দিয়ে নিবন্ধন করুন (`PUBLIC_URL` ও `WEBHOOK_SECRET` একই env-এ থাকতে হবে)।
4. Group-এ `/ask` লিখে bot উত্তর দিচ্ছে কিনা দেখুন।

### 7.5 UptimeRobot (Free plan-এর ঘুম এড়াতে)
Render Free service নিষ্ক্রিয় থাকলে ঘুমায়। UptimeRobot-এ HTTP(s) monitor বানান: URL `https://<service>.onrender.com/health`, interval 5 মিনিট।

---

## 8. Login কীভাবে কাজ করে

1. Browser `/login` খোলে। Server একটি nonce তৈরি করে `HttpOnly` cookie-তে রাখে (`ig_login_nonce`, ১০ মিনিট)।
2. Telegram Login Widget user-কে `/api/auth/telegram/callback?n=<nonce>&id=...&hash=...` এ পাঠায়।
3. Server:
   - Nonce cookie-র সাথে মেলায় (অন্য browser থেকে বানানো link কাজ করবে না — login CSRF প্রতিরোধ)
   - Field ছাড়া অন্য কোনো parameter থাকলে প্রত্যাখ্যান করে
   - HMAC-SHA256 যাচাই করে (secret = SHA256(bot token))
   - `auth_date` বৈধতা (`TELEGRAM_AUTH_MAX_AGE_SECONDS`) ও future skew যাচাই করে
   - একই payload দুইবার ব্যবহার হলে (replay) প্রত্যাখ্যান করে
   - Throttle যাচাই করে (IP ও user প্রতি)
   - `ADMIN_ID` হলে admin; নয়তো Bot API-তে group membership দেখে। সন্দেহ বা API ব্যর্থ হলে fail-closed
   - `suspended` বা `blocked` user প্রবেশ করতে পারবে না
4. সফল হলে `HttpOnly`, `Secure`, `SameSite` session cookie সেট হয় এবং `/` খোলে। Session-এর token শুধু hash হয়ে database-এ থাকে।

### Login error বার্তা (`/login?error=...`)

| Code | অর্থ |
|---|---|
| `login_expired` | Link মেয়াদোত্তীর্ণ, বা অন্য browser-এ খোলা হয়েছে। আবার চেষ্টা করুন |
| `login_failed` | Telegram যাচাই করতে পারেনি (ভুল hash, ভুল domain, টেম্পার) |
| `login_replayed` | একই login আগেই ব্যবহার হয়েছে। আবার login করুন |
| `not_member` | Group-এর সদস্য নন |
| `access_suspended` / `access_blocked` | Admin access বন্ধ করেছেন |
| `membership_unavailable` | Telegram এখন membership নিশ্চিত করতে পারছে না। পরে চেষ্টা করুন |
| `rate_limited` | অনেক চেষ্টা। কয়েক মিনিট অপেক্ষা করুন |

Error বার্তা বাঁধা (fixed) লেখা। URL-এর কোনো মান প্রতিধ্বনিত হয় না।

---

## 9. প্রথম Admin session (ধাপে ধাপে)

1. `ADMIN_ID` যে Telegram account-এর, সেই account-এ Telegram-এ থাকুন।
2. Browser-এ `https://<service>.onrender.com/login` খুলুন।
3. Telegram login button-এ ক্লিক করে login নিশ্চিত করুন।
4. `/` (chat) খুললে উপরে আপনার নাম দেখাবে। আপনি member না হলেও admin হিসেবে প্রবেশ পাবেন।
5. Browser-এ `https://<service>.onrender.com/admin_panel` খুলুন।
6. Overview-তে দেখুন: সক্রিয় session সংখ্যা, website conversation সংখ্যা, memory সংখ্যা, আজ ও এই মাসের request ও token ব্যবহার, এবং user সংখ্যা।
7. **Settings:** পরিবর্তন করে "Save changes" দিন। ফলাফল ট্যাবের নিচে দেখাবে: "Applied now" বা "Saved; restart the service to apply" (যা restart লাগে)।
8. **Users:** কোনো user খুঁজে তার status, quota বা model override বদলাতে পারবেন। প্রতিটি পরিবর্তন audit log-এ লেখা হয়।
9. **Memory & Q&A:** "Import & duplicates" → ফাইল বেছে "Preview" দিন। Preview-তে কতগুলো নতুন, কতগুলো আগে থেকে আছে দেখাবে। তারপর "Import N new record(s)" দিলে import হবে। Preview কোনো row লেখে না।
10. **Diagnostics:** Secret-এর শুধু "configured" বা "missing" দেখায়। মান কখনো দেখায় না।
11. Non-admin member `/admin_panel`-এ গেলে 403 পাবে। এটা নিশ্চিত করতে অন্য account দিয়ে যাচাই করুন।

---

## 10. Maintenance ও retention

- `services.run_maintenance` (একই throttled job, `MAINTENANCE_INTERVAL_SECONDS`, default ৬ ঘণ্টা) এখন:
  - মেয়াদোত্তীর্ণ session ও login replay মোছে
  - `WEB_CHAT_RETENTION_DAYS`-এর চেয়ে পুরনো website conversation মোছে (message-সহ, cascade)
- Bot-এর purging (question, log, cache) আগের মতোই চলে।
- Telegram bot থেকে `/maintenance` দিয়ে জোর করে চালানো যায়।
- Admin-এর কাছে ফলাফল: maintenance ফলের মধ্যে `web_purged_conversations`, `web_purged_sessions`, `web_purged_login_replays` থাকে।

---

## 11. নিরাপত্তা (সংক্ষেপে)

- **Authorization server-side:** browser থেকে আসা user ID, role, quota, membership বা admin flag কখনো বিশ্বাস করা হয় না। প্রতিটি request-এ session cookie থেকে server identity নেওয়া হয়।
- **Cookies:** `HttpOnly`, `Secure` (HTTPS-এ), `SameSite=Lax`। Session cookie-র নাম `__Host-` prefix-এ (HTTPS-এ)।
- **CSRF:** সব unsafe method-এ CSRF token ও Origin যাচাই। Token শুধু session-সাথে সই।
- **CSP:** `script-src 'self'` (সাথে Telegram widget-এর origin)। আমাদের script-এ inline script বা `eval` নেই, আর আমাদের template-এ inline `style=` নেই; style CSS class দিয়ে হয়। Telegram widget-এর iframe-এর নিজস্ব style তার নিজের।
- **XSS:** markdown নিজস্ব safe renderer দিয়ে HTML escape করে; `javascript:` link রেন্ডার হয় না; বাইরের link `rel="noopener noreferrer"` পায়।
- **Secrets:** frontend-এ কখনো যায় না। Dashboard শুধু "configured" দেখায়।
- **Limits:** AI provider call-এর আগে backend-এ যাচাই। Frontend-এর limit একমাত্র সুরক্ষা নয়।
- **Admin audit:** user পরিবর্তন, quota reset, session revoke, settings পরিবর্তন, memory তৈরি/সম্পাদনা/মোছা, import, dedupe, website conversation দেখা ও মোছা `admin_audit_log`-এ লেখা হয়।
- **Private chat ও memory আলাদা:** website chat কখনো memory বা shared answer cache-এ যায় না।
- **RLS:** নতুন ৮টি table-এ RLS চালু, কোনো public policy নেই।

---

## 12. Test চালানো

```bash
pip install -r requirements-dev.txt
TEST_DATABASE_URL=postgresql://postgres:<password>@localhost:5432/<test_db> \
  python -m pytest -o addopts="" -q tests                       # Python (DB সহ)
node --test tests/js/                                           # Markdown renderer (Node 18+)
```

`TEST_DATABASE_URL` না থাকলে DB-নির্ভর test বাদ যায়। Production database-এ test চালাবেন না; আলাদা test database ব্যবহার করুন।

---

## 13. সমস্যা ও সমাধান

| লক্ষণ | কারণ ও সমাধান |
|---|---|
| Login-এর পর `not_member` | Bot group-এ নেই, অথবা আপনি group-এর সদস্য নন। `GROUP_ID` ঠিক আছে কিনা দেখুন |
| `login_failed` | `/setdomain` ঠিক domain-এ দেওয়া হয়নি; অথবা `BOT_TOKEN` পরিবর্তন হয়েছে |
| Widget-এর বদলে "Login is not set up" | `data-auth-url` নেই; `/login` পুনরায় লোড করুন |
| সব login-এ `rate_limited` | একই IP থেকে অনেক চেষ্টা। `LOGIN_RATE_LIMIT_PER_WINDOW` বাড়ান বা অপেক্ষা করুন |
| `/admin_panel` 403 | আপনার Telegram ID `ADMIN_ID`-এর সাথে মেলে না |
| Chat-এ "The service is starting up" | Database বা responder প্রস্তুত নয়। `/health` ও log দেখুন |
| Migration `--status` বলছে pending | `python scripts/migrate.py` চালান |
| Render-এ service ঘুমিয়ে গেছে | UptimeRobot monitor যোগ করুন (7.5) |
| Cookie সেট হচ্ছে না (HTTP-তে) | `WEB_COOKIE_SECURE=true` হলে HTTP-তে cookie যায় না। লোকালে `false` করুন |

---

## 14. জানা সীমাবদ্ধতা

- AI উত্তর streaming নয়; সম্পূর্ণ হলে দেখায়।
- Telegram-এর আসল login widget দিয়ে end-to-end test এখনো করা হয়নি। স্থানীয় browser test-এ Telegram API-র stub ব্যবহার হয়েছে। Deploy-এর পর নিজের account দিয়ে login যাচাই করুন।
- Import preview-এর pending token ও login throttle process-এর ভেতরে থাকে। Render Free-তে একটি process চলে, তাই একাধিক instance-এ এগুলো ভাগ হবে না (এখন দরকার নেই)।
- Render Free plan-এ ঘুম থেকে ওঠার সময় প্রথম request ধীর হতে পারে।
- Rollback migration website data ও audit log মুছে দেয়।

---

## 15. পরিবর্তিত ও নতুন file

সম্পূর্ণ তালিকা `docs/WEB_PLATFORM_PLAN.md`-এর শেষ অংশে আছে।
