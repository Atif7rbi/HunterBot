"""Passenger entrypoint for HunterBot hosting.

Hosting rule:
- Passenger serves the dashboard only.
- No scanner/background/live-price loops are allowed inside the web process.
- Periodic work must run from cron commands.

This file intentionally imports the existing NiceGUI dashboard, then strips the
startup handlers registered by ui.app so Passenger does not start long-running
asyncio tasks such as _background_loop() or LivePriceTracker.loop().
"""
from __future__ import annotations

import os

# Flag for any hosting-aware modules/helpers added later.
os.environ.setdefault("HUNTER_HOSTING_READ_ONLY", "1")
os.environ.setdefault("HUNTER_RUNTIME_MODE", "hosting_read_only")

# Importing ui.app registers all routes/pages on the NiceGUI app.
from nicegui import app as nicegui_app  # noqa: E402
import ui.app as hunter_ui_app  # noqa: F401,E402
from src.core.database import init_db  # noqa: E402
from src.core.logger import logger  # noqa: E402


# NiceGUI uses an underlying FastAPI/Starlette application.
# ui.app currently registers startup tasks that launch infinite loops.
# In Passenger/cPanel those loops must not run in the web worker.
try:
    native = nicegui_app.native
    native.router.on_startup.clear()
except Exception as exc:  # pragma: no cover - defensive hosting guard
    logger.exception(f"Passenger startup cleanup failed: {exc}")


@nicegui_app.on_startup
async def passenger_read_only_startup() -> None:
    """Initialize database access only; do not start background jobs."""
    await init_db()
    logger.info("HunterBot Passenger startup: dashboard read-only mode")


# Passenger will look for this WSGI/ASGI callable.
# For NiceGUI this is the underlying ASGI application.
application = nicegui_app.native
app = application
