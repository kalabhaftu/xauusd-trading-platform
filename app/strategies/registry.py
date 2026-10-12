from app.strategies.ict_sweep_mss_fvg import IctSweepMssFvg
from app.strategies.support_resistance_bounce import SupportResistanceBounce

REGISTRY = {strategy.name: strategy for strategy in (IctSweepMssFvg, SupportResistanceBounce)}


def build(name: str, parameters: dict | None = None):
    if name not in REGISTRY:
        raise ValueError(f"Unknown strategy: {name}")
    return REGISTRY[name](parameters)
