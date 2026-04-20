import asyncio
import os
import signal
import structlog
import uvicorn
from polybot.core.config import Settings
from polybot.core.engine import Engine
from polybot.db.connection import Database
from polybot.markets.scanner import PolymarketScanner
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
from polybot.strategies.live_game import LiveGameCloserStrategy
from polybot.analysis.espn_client import ESPNClient

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ],
)
log = structlog.get_logger()


async def _run_bot_tasks(engine_fn, dashboard_fn, shutdown_event: asyncio.Event):
    """Run engine and dashboard concurrently. Dashboard failure is non-fatal."""
    engine_task = asyncio.create_task(engine_fn())
    dashboard_task = asyncio.create_task(dashboard_fn())
    shutdown_task = asyncio.create_task(shutdown_event.wait())

    def _on_dashboard_done(task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            log.error("dashboard_crashed", error=str(exc))

    dashboard_task.add_done_callback(_on_dashboard_done)

    # Wait for shutdown signal or engine exit (NOT dashboard exit)
    await asyncio.wait(
        [engine_task, shutdown_task],
        return_when=asyncio.FIRST_COMPLETED)

    # Cancel everything
    for task in [engine_task, dashboard_task, shutdown_task]:
        task.cancel()
    results = await asyncio.gather(engine_task, dashboard_task, shutdown_task, return_exceptions=True)
    engine_result = results[0]
    if isinstance(engine_result, BaseException) and not isinstance(engine_result, asyncio.CancelledError):
        log.error("engine_crashed", error=str(engine_result))
        raise engine_result


async def main():
    settings = Settings()
    log.info("polybot_starting", bankroll=settings.starting_bankroll)
    espn_client = None
    db = Database(settings.database_url)
    await db.connect()
    exists = await db.fetchval("SELECT COUNT(*) FROM system_state")
    if exists == 0:
        await db.execute("INSERT INTO system_state (bankroll) VALUES ($1)", settings.starting_bankroll)
    scanner = PolymarketScanner(api_key=settings.polymarket_api_key)
    await scanner.start()
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

    # Run preflight checks before starting live trading
    if not settings.dry_run:
        import subprocess
        log.info("running_live_preflight")
        result = subprocess.run(
            ["uv", "run", "python", "scripts/live_preflight.py"],
            capture_output=True, text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        print(result.stdout)
        if result.returncode != 0:
            log.critical("PREFLIGHT_FAILED — refusing to start live trading")
            if result.stderr:
                log.error("preflight_stderr", output=result.stderr[-500:])
            return
        log.info("preflight_passed")

    recorder = TradeRecorder(
        db=db, cold_start_trades=settings.cold_start_trades,
        brier_ema_alpha=settings.brier_ema_alpha)
    trade_learner = TradeLearner(db=db, settings=settings)
    executor = OrderExecutor(
        scanner=scanner, wallet=wallet, db=db,
        fill_timeout_seconds=settings.fill_timeout_seconds,
        clob=clob, dry_run=settings.dry_run,
        trade_learner=trade_learner)
    executor._settings = settings
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

    price_history_scanner = None
    engine = Engine(
        db=db, scanner=scanner, researcher=None, ensemble=None,
        executor=executor, recorder=recorder, risk_manager=risk_manager,
        settings=settings, email_notifier=email_notifier,
        position_manager=position_manager, clob=clob,
        portfolio_lock=portfolio_lock, trade_learner=trade_learner,
        price_history_scanner=price_history_scanner)

    log.info("polybot_mode", dry_run=settings.dry_run, clob_connected=clob is not None)

    engine.add_strategy(ResolutionSnipeStrategy(
        settings=settings, ensemble=None, odds_client=None))

    if getattr(settings, 'lg_enabled', False):
        espn_client = ESPNClient(
            sports=getattr(settings, 'lg_sports', 'mlb,nba,nhl').split(','))
        await espn_client.start()
        lg_strategy = LiveGameCloserStrategy(settings=settings, espn_client=espn_client)
        engine.add_strategy(lg_strategy)

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

    try:
        await _run_bot_tasks(engine.run_forever, dashboard_server.serve, shutdown_event)
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
        for client in [scanner, espn_client]:
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    pass
        await db.close()
        log.info("polybot_shutdown_complete")


if __name__ == "__main__":
    asyncio.run(main())
