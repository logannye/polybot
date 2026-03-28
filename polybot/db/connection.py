import asyncpg
import structlog
from pathlib import Path

log = structlog.get_logger()

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class Database:
    def __init__(self, database_url: str):
        self._url = database_url
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self._url, min_size=2, max_size=10)
        await self._apply_schema()
        log.info("database_connected", url=self._url.split("@")[-1])

    async def _apply_schema(self) -> None:
        schema = SCHEMA_PATH.read_text()
        async with self._pool.acquire() as conn:
            await conn.execute(schema)

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            log.info("database_closed")

    def acquire(self):
        return self._pool.acquire()

    async def fetchrow(self, query: str, *args):
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetch(self, query: str, *args):
        async with self._pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def fetchval(self, query: str, *args):
        async with self._pool.acquire() as conn:
            return await conn.fetchval(query, *args)

    async def execute(self, query: str, *args):
        async with self._pool.acquire() as conn:
            return await conn.execute(query, *args)
