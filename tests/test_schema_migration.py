"""Tests for schema migration safety in _apply_schema()."""
import pytest
from unittest.mock import AsyncMock, MagicMock


class FakeTransaction:
    """Async context manager that tracks whether it was entered."""

    def __init__(self):
        self.entered = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *args):
        return False


def make_mock_pool(mock_conn):
    """Build a minimal asyncpg-style pool mock that yields mock_conn."""
    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return mock_pool


@pytest.mark.asyncio
async def test_apply_schema_calls_execute():
    """_apply_schema() must call conn.execute with the schema SQL."""
    from polybot.db.connection import Database

    db = Database("postgresql://localhost/polybot_test")

    fake_txn = FakeTransaction()
    mock_conn = MagicMock()
    # transaction() is a sync call in asyncpg; it returns an async context manager
    mock_conn.transaction = MagicMock(return_value=fake_txn)
    mock_conn.execute = AsyncMock()

    db._pool = make_mock_pool(mock_conn)

    await db._apply_schema()

    mock_conn.execute.assert_called_once()
    sql_arg = mock_conn.execute.call_args[0][0]
    assert "CREATE TABLE IF NOT EXISTS trades" in sql_arg


@pytest.mark.asyncio
async def test_apply_schema_uses_transaction():
    """_apply_schema() must wrap conn.execute inside a transaction."""
    from polybot.db.connection import Database

    db = Database("postgresql://localhost/polybot_test")

    fake_txn = FakeTransaction()
    execute_called_inside_transaction = False

    mock_conn = MagicMock()
    mock_conn.transaction = MagicMock(return_value=fake_txn)

    async def fake_execute(sql, *args):
        nonlocal execute_called_inside_transaction
        # At the point execute runs, the transaction context must already be active
        execute_called_inside_transaction = fake_txn.entered

    mock_conn.execute = AsyncMock(side_effect=fake_execute)

    db._pool = make_mock_pool(mock_conn)

    await db._apply_schema()

    assert fake_txn.entered, "transaction() context manager was never entered"
    assert execute_called_inside_transaction, (
        "conn.execute() was not called inside the transaction"
    )
