from __future__ import annotations
import asyncio
import logging
from .config import get_settings
from .db import SessionLocal
from .service import JobService

logger = logging.getLogger(__name__)


async def worker_loop(stop_event: asyncio.Event) -> None:
    settings = get_settings()
    service = JobService()
    startup_diagnostics_logged = False
    while not stop_event.is_set():
        try:
            with SessionLocal() as db:
                if not startup_diagnostics_logged:
                    try:
                        service.log_local_staging_diagnostics(db)
                    except Exception:
                        logger.exception("Startup local staging diagnostics failed")
                    else:
                        startup_diagnostics_logged = True
                service.process_nonterminal_jobs(db)
        except Exception:
            logger.exception("Background worker cycle failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.polling_interval_seconds)
        except asyncio.TimeoutError:
            pass
