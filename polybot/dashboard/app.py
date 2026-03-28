from fastapi import FastAPI
from datetime import datetime, timezone


def create_app(db) -> FastAPI:
    app = FastAPI(title="Polybot Dashboard")

    @app.get("/health")
    async def health():
        state = await db.fetchrow("SELECT * FROM system_state WHERE id = 1")
        if not state:
            return {"status": "error", "message": "no system state"}
        return {
            "status": "ok",
            "bankroll": float(state["bankroll"]),
            "last_scan": str(state["last_scan_at"]),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    @app.get("/")
    async def root():
        state = await db.fetchrow("SELECT * FROM system_state WHERE id = 1")
        if not state:
            return {"error": "no system state"}

        open_trades = await db.fetch(
            """SELECT t.*, m.question, m.category FROM trades t
               JOIN markets m ON t.market_id = m.id
               WHERE t.status = 'open' ORDER BY t.opened_at DESC"""
        )

        return {
            "bankroll": float(state["bankroll"]),
            "total_deployed": float(state["total_deployed"]),
            "daily_pnl": float(state["daily_pnl"]),
            "kelly_mult": float(state["kelly_mult"]),
            "edge_threshold": float(state["edge_threshold"]),
            "circuit_breaker_until": str(state["circuit_breaker_until"]),
            "last_scan_at": str(state["last_scan_at"]),
            "open_positions": [
                {
                    "id": t["id"],
                    "market": t["question"],
                    "category": t["category"],
                    "side": t["side"],
                    "entry_price": float(t["entry_price"]),
                    "size_usd": float(t["position_size_usd"]),
                    "opened_at": str(t["opened_at"]),
                }
                for t in open_trades
            ],
        }

    @app.get("/trades")
    async def trades():
        rows = await db.fetch(
            """SELECT t.*, m.question, m.category FROM trades t
               JOIN markets m ON t.market_id = m.id
               ORDER BY t.opened_at DESC LIMIT 50"""
        )
        return [
            {
                "id": t["id"],
                "market": t["question"],
                "category": t["category"],
                "side": t["side"],
                "entry_price": float(t["entry_price"]),
                "exit_price": float(t["exit_price"]) if t["exit_price"] else None,
                "size_usd": float(t["position_size_usd"]),
                "pnl": float(t["pnl"]) if t["pnl"] else None,
                "status": t["status"],
                "exit_reason": t["exit_reason"],
                "opened_at": str(t["opened_at"]),
                "closed_at": str(t["closed_at"]) if t["closed_at"] else None,
            }
            for t in rows
        ]

    @app.get("/models")
    async def models():
        rows = await db.fetch("SELECT * FROM model_performance ORDER BY trust_weight DESC")
        return [
            {
                "model": r["model_name"],
                "brier_score": float(r["brier_score_ema"]),
                "trust_weight": float(r["trust_weight"]),
                "resolved_count": r["resolved_count"],
                "last_updated": str(r["last_updated"]),
            }
            for r in rows
        ]

    return app
