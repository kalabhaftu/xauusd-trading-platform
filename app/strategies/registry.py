from app.strategies.base import Parameters
from app.strategies.mean_reversion import MeanReversion
from app.strategies.session_breakout import SessionBreakout
from app.strategies.trend_pullback import TrendPullback

REGISTRY = {strategy.name: strategy for strategy in (TrendPullback, SessionBreakout, MeanReversion)}


def build(name: str, parameters: dict | None = None):
    if name not in REGISTRY:
        raise ValueError(f"Unknown strategy: {name}")
    return REGISTRY[name](Parameters(**(parameters or {})))
