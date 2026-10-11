import math

import numpy as np
import pandas as pd


def finite(value):
    return float(value) if value is not None and np.isfinite(value) else None


def streak(values, positive: bool) -> int:
    best = current = 0
    for value in values:
        current = current + 1 if (value > 0 if positive else value < 0) else 0
        best = max(best, current)
    return best


def metrics(trades, equity, capital, cost, multiplier):
    series = equity.Equity
    n = len(trades)
    pnl = trades.PnL if n else pd.Series(dtype=float)
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    drawdown = 1 - series / series.cummax().clip(lower=capital)
    maximum = float(drawdown.max())
    elapsed_days = (series.index[-1] - series.index[0]).total_seconds() / 86400
    daily = series.resample("1D").last().dropna()
    returns = daily.pct_change().dropna()
    meaningful = len(returns) >= 60
    sd = returns.std(ddof=1)
    downside = np.sqrt(np.mean(np.minimum(returns, 0) ** 2)) if len(returns) else 0
    final = float(series.iloc[-1])
    annual = (final / capital) ** (365.25 / elapsed_days) - 1 \
        if elapsed_days >= 365 and final > 0 else None
    monthly_equity = series.resample("ME").last()
    monthly = monthly_equity.pct_change()
    if len(monthly):
        monthly.iloc[0] = monthly_equity.iloc[0] / capital - 1
    exposure = np.zeros(len(series), dtype=bool)
    if n:
        for trade in trades.itertuples():
            exposure |= (series.index >= trade.EntryTime) & (series.index <= trade.ExitTime)
    costs = float(trades.Commission.sum()) if n and "Commission" in trades else 0
    if n:
        costs += float((trades.VolumeLots * (cost.spread_price + 2 * cost.slippage_price)
                        * (multiplier / (trades.VolumeLots / trades.Size.abs()).iloc[0])).sum())
        costs += float(-trades.Financing.clip(upper=0).sum())
    groups = {}
    if n:
        for field in ("session", "strategy", "regime"):
            groups[field] = {str(key): {"trades": int(len(group)), "pnl": float(group.PnL.sum()),
                                       "expectancy": float(group.PnL.mean())}
                             for key, group in trades.groupby(field)}
    return {
        "initial_capital": capital, "final_equity": final,
        "sample_start": series.index[0].isoformat(), "sample_end": series.index[-1].isoformat(),
        "total_net_return": final / capital - 1, "annualized_return": finite(annual), "total_trades": n,
        "win_rate": len(wins) / n if n else None,
        "average_win": finite(wins.mean()), "average_loss": finite(losses.mean()),
        "profit_factor": finite(wins.sum() / -losses.sum()) if len(losses) else None,
        "expectancy": finite(pnl.mean()), "expectancy_r": finite(trades.R.mean()) if n else None,
        "maximum_drawdown": maximum, "recovery_factor": finite((final / capital - 1) / maximum) if maximum else None,
        "sharpe": finite(returns.mean() / sd * math.sqrt(365.25)) if meaningful and sd > 0 else None,
        "sortino": finite(returns.mean() / downside * math.sqrt(365.25)) if meaningful and downside > 0 else None,
        "average_holding_seconds": finite(trades.Duration.dt.total_seconds().mean()) if n else None,
        "consecutive_wins": streak(pnl, True), "consecutive_losses": streak(pnl, False),
        "exposure_time": float(exposure.mean()), "estimated_transaction_costs": costs,
        "monthly_returns": {str(k.date()): finite(v) for k, v in monthly.items()},
        "performance_by": groups,
        "largest_win_share": finite(wins.max() / wins.sum()) if len(wins) and wins.sum() else None,
        "undefined_metrics_policy": "null denotes insufficient or degenerate sample; risk-free rate assumed zero",
    }
