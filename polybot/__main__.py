import asyncio
import signal
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
from polybot.trading.clob import ClobGateway
from polybot.learning.recorder import TradeRecorder
from polybot.learning.trade_learning import TradeLearner
from polybot.notifications.email import EmailNotifier
from polybot.trading.position_manager import ActivePositionManager
from polybot.dashboard.app import create_app
from polybot.strategies.snipe import ResolutionSnipeStrategy
from polybot.strategies.forecast import EnsembleForecastStrategy
from polybot.strategies.market_maker import MarketMakerStrategy
from polybot.strategies.mean_reversion import MeanReversionStrategy

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
    clob = None
    if settings.polymarket_api_secret and settings.polymarket_api_passphrase:
        clob = ClobGateway(
            host="https://clob.polymarket.com",
            chain_id=settings.polymarket_chain_id,
            private_key=settings.polymarket_private_key,
            api_key=settings.polymarket_api_key,
            api_secret=settings.polymarket_api_secret,
            api_passphrase=settings.polymarket_api_passphrase)

    if not settings.dry_run and clob is None:
        log.error("live_mode_requires_clob_credentials")
        return
    recorder = TradeRecorder(
        db=db, cold_start_trades=settings.cold_start_trades,
        brier_ema_alpha=settings.brier_ema_alpha)
    trade_learner = TradeLearner(db=db, settings=settings)
    executor = OrderExecutor(
        scanner=scanner, wallet=wallet, db=db,
        fill_timeout_seconds=settings.fill_timeout_seconds,
        clob=clob, dry_run=settings.dry_run,
        trade_learner=trade_learner)
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
        api_key=settings.resend_api_key, to_email=settings.alert_email,
        dry_run=settings.dry_run)
    portfolio_lock = asyncio.Lock()
    position_manager = ActivePositionManager(
        db=db, executor=executor, scanner=scanner,
        email_notifier=email_notifier, settings=settings,
        portfolio_lock=portfolio_lock)

    engine = Engine(
        db=db, scanner=scanner, researcher=researcher, ensemble=ensemble,
        executor=executor, recorder=recorder, risk_manager=risk_manager,
        settings=settings, email_notifier=email_notifier,
        position_manager=position_manager, clob=clob,
        portfolio_lock=portfolio_lock, trade_learner=trade_learner)

    log.info("polybot_mode", dry_run=settings.dry_run, clob_connected=clob is not None)

    engine.add_strategy(ResolutionSnipeStrategy(settings=settings, ensemble=ensemble))
    engine.add_strategy(EnsembleForecastStrategy(
        settings=settings, ensemble=ensemble, researcher=researcher))

    if settings.mm_enabled:
        mm_strategy = MarketMakerStrategy(
            settings=settings, clob=clob, scanner=scanner,
            dry_run=settings.dry_run)
        engine.add_strategy(mm_strategy)

    if getattr(settings, 'mr_enabled', False):
        mr_strategy = MeanReversionStrategy(settings=settings)
        engine.add_strategy(mr_strategy)

    app = create_app(db)
    dashboard_server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=8080, log_level="warning"))

    # Graceful shutdown on signals
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _signal_handler(sig):
        log.info("shutdown_signal_received", signal=sig.name)
        shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler, sig)

    engine_task = asyncio.create_task(engine.run_forever())
    dashboard_task = asyncio.create_task(dashboard_server.serve())

    try:
        # Wait until shutdown signal or engine exits
        done, pending = await asyncio.wait(
            [engine_task, dashboard_task, asyncio.create_task(shutdown_event.wait())],
            return_when=asyncio.FIRST_COMPLETED)
        # Cancel remaining tasks
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        # Log open positions on shutdown
        try:
            open_trades = await db.fetch(
                """SELECT t.id, t.strategy, t.side, t.position_size_usd, m.question
                   FROM trades t JOIN markets m ON t.market_id = m.id
                   WHERE t.status IN ('open', 'filled', 'dry_run')""")
            log.info("shutdown_open_positions", count=len(open_trades),
                     positions=[{"id": t["id"], "strategy": t["strategy"],
                                 "side": t["side"], "size": float(t["position_size_usd"])}
                                for t in open_trades])
        except Exception:
            pass
        await scanner.close()
        await researcher.close()
        await db.close()
        log.info("polybot_shutdown_complete")


if __name__ == "__main__":
    asyncio.run(main())
