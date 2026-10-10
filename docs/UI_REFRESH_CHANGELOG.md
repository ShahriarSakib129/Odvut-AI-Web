# Odvut AI UI refresh changelog

## Updated
- `web/templates/chat.html`: removed the Admin panel link from the AI chat header; refreshed Odvut AI branding and chat copy.
- `web/templates/login.html`: refreshed login page branding and access copy.
- `web/templates/base.html`: added theme color and page description metadata.
- `web/templates/admin.html`: refreshed admin sidebar branding.
- `web/static/css/app.css`: added a responsive dark navy/cyan/violet visual refresh for chat, login, and admin views; no external assets/CDNs added.
- `bot_telegram/handlers.py`: `/ping` response now uses the English term `Pong!` instead of the incorrect Bengali transliteration.
- `bot_telegram/permissions.py`: target admin can trigger answers by mentioning the bot or replying to its message, while generic keyword triggers remain disabled for admin messages. The existing `/ask` command remains available.

## Validation
- Python compile check completed successfully.
- Automated tests could not be collected in this environment because runtime dependencies (`Flask` and `python-telegram-bot`) are not installed. Install `requirements.txt` and run `pytest -q` before deploying.

## Important security note
The existing Telegram-authenticated admin access has not been replaced with a separate password gate in this refresh. Do not assume a new admin-panel password has been added. Implement and test a server-side password gate with hashed password storage and a change-password flow before treating that requirement as complete.
