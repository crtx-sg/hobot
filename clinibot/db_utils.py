"""SQLite retry helper for SQLITE_BUSY resilience."""

import asyncio


async def retry_execute(db, sql, params=None, retries=3, backoff=0.1):
    """Execute+commit with retry on SQLITE_BUSY."""
    for attempt in range(retries):
        try:
            cur = await db.execute(sql, params)
            await db.commit()
            return cur
        except Exception as exc:
            if "database is locked" in str(exc) and attempt < retries - 1:
                await asyncio.sleep(backoff * (attempt + 1))
            else:
                raise
