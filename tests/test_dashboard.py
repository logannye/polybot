import pytest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
from polybot.dashboard.app import create_app


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.fetchrow = AsyncMock(return_value={
        "bankroll": 315.0,
        "total_deployed": 45.0,
        "daily_pnl": 15.0,
        "kelly_mult": 0.25,
        "edge_threshold": 0.05,
        "last_scan_at": "2026-03-27T12:00:00+00:00",
        "circuit_breaker_until": None,
    })
    db.fetch = AsyncMock(return_value=[])
    return db


@pytest.fixture
def client(mock_db):
    app = create_app(mock_db)
    return TestClient(app)


class TestDashboard:
    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "bankroll" in data

    def test_root(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        data = resp.json()
        assert "bankroll" in data
        assert data["bankroll"] == 315.0

    def test_trades(self, client):
        resp = client.get("/trades")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_models(self, client, mock_db):
        mock_db.fetch = AsyncMock(return_value=[
            {"model_name": "claude-sonnet-4.6", "brier_score_ema": 0.20,
             "trust_weight": 0.4, "resolved_count": 10, "last_updated": "2026-03-27"},
        ])
        resp = client.get("/models")
        assert resp.status_code == 200

    def test_strategies(self, client, mock_db):
        mock_db.fetch = AsyncMock(return_value=[
            {"strategy": "arbitrage", "total_trades": 10, "winning_trades": 8,
             "total_pnl": 5.0, "avg_edge": 0.03, "enabled": True,
             "last_updated": "2026-03-28T00:00:00Z"},
        ])
        resp = client.get("/strategies")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["strategy"] == "arbitrage"
        assert data[0]["enabled"] is True

    def test_arb(self, client, mock_db):
        mock_db.fetch = AsyncMock(return_value=[
            {"id": 1, "question": "Test arb?", "side": "YES", "entry_price": 0.45,
             "position_size_usd": 10.0, "pnl": 0.50, "status": "closed",
             "opened_at": "2026-03-28T00:00:00Z"},
        ])
        resp = client.get("/arb")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["market"] == "Test arb?"
