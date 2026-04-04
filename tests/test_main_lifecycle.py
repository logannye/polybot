import asyncio
import pytest


@pytest.mark.asyncio
async def test_dashboard_crash_does_not_kill_engine():
    """If the dashboard task raises, the engine task must continue running."""
    engine_iterations = 0

    async def fake_engine_run():
        nonlocal engine_iterations
        while engine_iterations < 3:
            engine_iterations += 1
            await asyncio.sleep(0.01)

    async def fake_dashboard_serve():
        raise OSError("Address already in use")

    from polybot.__main__ import _run_bot_tasks
    shutdown_event = asyncio.Event()

    async def auto_shutdown():
        while engine_iterations < 3:
            await asyncio.sleep(0.01)
        shutdown_event.set()

    asyncio.create_task(auto_shutdown())
    await _run_bot_tasks(fake_engine_run, fake_dashboard_serve, shutdown_event)
    assert engine_iterations == 3


@pytest.mark.asyncio
async def test_shutdown_signal_stops_engine():
    """Setting the shutdown event must cancel engine and dashboard."""
    engine_started = asyncio.Event()

    async def fake_engine_run():
        engine_started.set()
        await asyncio.sleep(100)

    async def fake_dashboard_serve():
        await asyncio.sleep(100)

    from polybot.__main__ import _run_bot_tasks
    shutdown_event = asyncio.Event()

    async def trigger_shutdown():
        await engine_started.wait()
        shutdown_event.set()

    asyncio.create_task(trigger_shutdown())
    await _run_bot_tasks(fake_engine_run, fake_dashboard_serve, shutdown_event)
