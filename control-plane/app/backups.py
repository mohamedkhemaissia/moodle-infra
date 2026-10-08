"""Lecture asynchrone du statut public des sauvegardes."""
import asyncio
from datetime import datetime, timezone
from .backup_status import summary


async def read_summary(path):
    return await asyncio.to_thread(summary, path, datetime.now(timezone.utc))
