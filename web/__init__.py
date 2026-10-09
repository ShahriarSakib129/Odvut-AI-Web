"""Website layer: Telegram-authenticated AI chat and protected admin dashboard.

The website never owns AI logic. It calls the same ``services.responder``
pipeline that the Telegram handlers use, so memory, Q&A retrieval, prompts,
model configuration and usage logging are shared.

Entry point: ``web.blueprint.register`` (called once from ``app.create_app``).
"""

