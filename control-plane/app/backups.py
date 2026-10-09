"""Lecture asynchrone du statut public des sauvegardes."""
import asyncio
from datetime import datetime, timezone
from .backup_status import summary
from .pilotage_backup_status import summary as pilotage_summary


async def read_summary(path):
    return await asyncio.to_thread(summary, path, datetime.now(timezone.utc))


async def read_pilotage_summary(path):
    return await asyncio.to_thread(pilotage_summary, path, datetime.now(timezone.utc))
