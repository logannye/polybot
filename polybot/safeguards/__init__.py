"""Safeguard layer — halt/divergence/stage checks, extracted from engine.py
per v10 spec §2. Each safeguard is a single-responsibility class consumed
by ``polybot.core.engine.Engine`` via dependency injection.
"""

from polybot.safeguards.drawdown_halt import DrawdownHalt
from polybot.safeguards.capital_divergence import CapitalDivergenceMonitor
from polybot.safeguards.deployment_stage import DeploymentStageGate

__all__ = ["DrawdownHalt", "CapitalDivergenceMonitor", "DeploymentStageGate"]
