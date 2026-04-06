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
from polybot.markets.price_history import PriceHistoryScanner
from polybot.analysis.odds_client import OddsClient
from polybot.strategies.cross_venue import CrossVenueStrategy
from polybot.strategies.political import PoliticalStrategy

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
    db = Database(settings.database_url)
    await db.connect()
    exists = await db.fetchval("SELECT COUNT(*) FROM system_state")
    if exists == 0:
        await db.execute("INSERT INTO system_state (bankroll) VALUES ($1)", settings.starting_bankroll)
    await db.execute(
        """INSERT INTO strategy_performance (strategy, total_trades, winning_trades, total_pnl, avg_edge, enabled)
           VALUES ('cross_venue', 0, 0, 0, 0, true) ON CONFLICT (strategy) DO NOTHING""")
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

    price_history_scanner = None
    engine = Engine(
        db=db, scanner=scanner, researcher=researcher, ensemble=ensemble,
        executor=executor, recorder=recorder, risk_manager=risk_manager,
        settings=settings, email_notifier=email_notifier,
        position_manager=position_manager, clob=clob,
        portfolio_lock=portfolio_lock, trade_learner=trade_learner,
        price_history_scanner=price_history_scanner)

    log.info("polybot_mode", dry_run=settings.dry_run, clob_connected=clob is not None)

    _snipe_odds = None
    if getattr(settings, 'snipe_odds_verification_enabled', False) and getattr(settings, 'odds_api_key', ''):
        if 'odds_client' in dir():
            _snipe_odds = odds_client
        else:
            from polybot.analysis.odds_client import OddsClient as _OC
            _snipe_odds = _OC(api_key=settings.odds_api_key)
            await _snipe_odds.start()
    engine.add_strategy(ResolutionSnipeStrategy(
        settings=settings, ensemble=ensemble, odds_client=_snipe_odds))
    if getattr(settings, 'forecast_enabled', True):
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
        price_history_scanner = PriceHistoryScanner(
            scanner=scanner,
            min_volume=settings.mr_min_volume_24h,
            move_threshold=settings.mr_trigger_threshold,
            max_markets=getattr(settings, 'mr_history_max_markets', 500),
            concurrency=getattr(settings, 'mr_history_concurrency', 50),
        )
        engine._price_history_scanner = price_history_scanner

    if getattr(settings, 'cv_enabled', False) and getattr(settings, 'odds_api_key', ''):
        odds_client = OddsClient(
            api_key=settings.odds_api_key,
            sports=getattr(settings, 'cv_sports', 'basketball_nba,icehockey_nhl').split(','))
        await odds_client.start()
        cv_strategy = CrossVenueStrategy(settings=settings, odds_client=odds_client)
        engine.add_strategy(cv_strategy)

    if getattr(settings, 'pol_enabled', True):
        pol_strategy = PoliticalStrategy(settings=settings)
        engine.add_strategy(pol_strategy)
        await db.execute(
            """INSERT INTO strategy_performance (strategy, total_trades, winning_trades, total_pnl, avg_edge, enabled)
               VALUES ('political', 0, 0, 0, 0, true) ON CONFLICT (strategy) DO NOTHING""")

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
        await scanner.close()
        await researcher.close()
        await db.close()
        log.info("polybot_shutdown_complete")


if __name__ == "__main__":
    asyncio.run(main())
