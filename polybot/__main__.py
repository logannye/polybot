import asyncio
import structlog
import uvicorn
from polybot.core.config import Settings
from polybot.core.engine import Engine
from polybot.db.connection import Database
from polybot.markets.scanner import PolymarketScanner
from polybot.analysis.research import BraveResearcher
from polybot.analysis.ensemble import EnsembleAnalyzer
from polybot.trading.executor import OrderExecutor
from polybot.trading.wallet import WalletManager
from polybot.trading.risk import RiskManager
from polybot.learning.recorder import TradeRecorder
from polybot.notifications.email import EmailNotifier
from polybot.markets.websocket import PositionTracker
from polybot.dashboard.app import create_app
from polybot.strategies.arbitrage import ArbitrageStrategy
from polybot.strategies.snipe import ResolutionSnipeStrategy
from polybot.strategies.forecast import EnsembleForecastStrategy

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ],
)
log = structlog.get_logger()


async def main():
    settings = Settings()
    log.info("polybot_starting", bankroll=settings.starting_bankroll)
    db = Database(settings.database_url)
    await db.connect()
    exists = await db.fetchval("SELECT COUNT(*) FROM system_state")
    if exists == 0:
        await db.execute("INSERT INTO system_state (bankroll) VALUES ($1)", settings.starting_bankroll)
    scanner = PolymarketScanner(api_key=settings.polymarket_api_key)
    await scanner.start()
    researcher = BraveResearcher(api_key=settings.brave_api_key)
    await researcher.start()
    ensemble = EnsembleAnalyzer(
        anthropic_key=settings.anthropic_api_key,
        openai_key=settings.openai_api_key,
        google_key=settings.google_api_key)
    wallet = WalletManager(private_key=settings.polymarket_private_key)
    executor = OrderExecutor(
        scanner=scanner, wallet=wallet, db=db,
        fill_timeout_seconds=settings.fill_timeout_seconds)
    recorder = TradeRecorder(
        db=db, cold_start_trades=settings.cold_start_trades,
        brier_ema_alpha=settings.brier_ema_alpha)
    risk_manager = RiskManager(
        max_single_pct=settings.max_single_position_pct,
        max_total_deployed_pct=settings.max_total_deployed_pct,
        max_per_category_pct=settings.max_per_category_pct,
        max_concurrent=settings.max_concurrent_positions,
        daily_loss_limit_pct=settings.daily_loss_limit_pct,
        circuit_breaker_hours=settings.circuit_breaker_hours,
        min_trade_size=settings.min_trade_size,
        book_depth_max_pct=settings.book_depth_max_pct)
    email_notifier = EmailNotifier(
        api_key=settings.resend_api_key, to_email=settings.alert_email)
    position_manager = PositionTracker(
        on_early_exit=lambda tid, p: executor.close_position(tid, p, "early_exit", 0, 0, "YES"),
        on_stop_loss=lambda tid, p: executor.close_position(tid, p, "stop_loss", 0, 0, "YES"))

    engine = Engine(
        db=db, scanner=scanner, researcher=researcher, ensemble=ensemble,
        executor=executor, recorder=recorder, risk_manager=risk_manager,
        settings=settings, email_notifier=email_notifier,
        position_manager=position_manager)

    engine.add_strategy(ArbitrageStrategy(settings=settings))
    engine.add_strategy(ResolutionSnipeStrategy(settings=settings, ensemble=ensemble))
    engine.add_strategy(EnsembleForecastStrategy(
        settings=settings, ensemble=ensemble, researcher=researcher))

    app = create_app(db)
    dashboard_server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=8080, log_level="warning"))

    try:
        await asyncio.gather(engine.run_forever(), dashboard_server.serve())
    finally:
        await scanner.close()
        await researcher.close()
        await db.close()
        log.info("polybot_shutdown")


if __name__ == "__main__":
    asyncio.run(main())
