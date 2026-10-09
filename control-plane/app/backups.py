"""Lecture asynchrone du statut public des sauvegardes."""
import asyncio
from datetime import datetime, timezone
from .backup_status import summary
from .pilotage_backup_status import summary as pilotage_summary
from .backup_catalog_status import summary as catalog_summary


async def read_summary(path):
    return await asyncio.to_thread(summary, path, datetime.now(timezone.utc))


async def read_pilotage_summary(path):
    return await asyncio.to_thread(pilotage_summary, path, datetime.now(timezone.utc))


async def read_catalog(path, scope, limit, offset):
    return await asyncio.to_thread(catalog_summary, path, datetime.now(timezone.utc), scope, limit, offset)
