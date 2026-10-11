import math
from decimal import ROUND_FLOOR, Decimal

from app.domain import Direction, SymbolSpec


def round_volume_down(value: float, spec: SymbolSpec) -> float:
    step = Decimal(str(spec.volume_step))
    volume = min(Decimal(str(value)), Decimal(str(spec.volume_max)))
    return float((volume / step).to_integral_value(rounding=ROUND_FLOOR) * step)


def size(broker, spec: SymbolSpec, direction: Direction, entry: float, stop: float,
         budget: float, cost_per_lot: float) -> tuple[float, float]:
    loss_per_lot = -broker.profit(spec.symbol, direction, 1.0, entry, stop) + cost_per_lot
    if not math.isfinite(loss_per_lot) or loss_per_lot <= 0 or budget <= 0:
        raise ValueError("Invalid broker loss calculation or risk budget")
    volume = round_volume_down(budget / loss_per_lot, spec)
    if volume < spec.volume_min:
        raise ValueError("Minimum broker lot exceeds risk budget")
    risk = -broker.profit(spec.symbol, direction, volume, entry, stop) + volume * cost_per_lot
    while risk > budget and volume >= spec.volume_min:
        volume = round_volume_down(volume - spec.volume_step, spec)
        risk = -broker.profit(spec.symbol, direction, volume, entry, stop) + volume * cost_per_lot
    if volume < spec.volume_min or risk <= 0:
        raise ValueError("No valid volume within risk budget")
    return volume, risk
