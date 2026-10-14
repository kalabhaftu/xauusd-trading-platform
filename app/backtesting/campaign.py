"""Exploratory strategy comparison across every requested entry interval."""

import html
import json
import uuid
import zipfile
from pathlib import Path
from typing import Literal

import pandas as pd

from app.backtesting.engine import BacktestRiskLimits, run_backtest
from app.backtesting.metrics import finite
from app.backtesting.reports import export_report
from app.data.validation import BACKTEST_TIMEFRAMES, TIMEFRAMES
from app.domain import utcnow
from app.strategies.registry import build

DEFAULT_TIMEFRAMES = BACKTEST_TIMEFRAMES


def _segments(trades, first, last):
    """Descriptive time splits; these are inspected research, never an untouched holdout."""
    boundaries = [first, first + (last - first) * .6, first + (last - first) * .8, last]
    output = {}
    for label, left, right in zip(("early_60_percent", "middle_20_percent", "late_20_percent"),
                                  boundaries[:-1], boundaries[1:]):
        part = trades.loc[(trades.EntryTime >= left) & (trades.EntryTime < right)] if len(trades) else trades
        pnl = part.PnL if len(part) else pd.Series(dtype=float)
        losses = pnl[pnl < 0]
        output[label] = {"trades": len(part), "net_pnl": float(pnl.sum()),
                         "profit_factor": finite(pnl[pnl > 0].sum() / -losses.sum()) if len(losses) else None,
                         "expectancy_r": finite(part.R.mean()) if len(part) else None}
    return output


def _rule_variants(name, parameters, timeframe):
    if name == "ict_sweep_mss_fvg":
        return [(f"gap_confirmation_{delay}", {**parameters, "fvg_confirmation_bars": delay})
                for delay in (0, 1, 2) if delay != parameters["fvg_confirmation_bars"]]
    variants = []
    effective = max(parameters["minimum_window_bars"],
                    (parameters["level_window_minutes"] + TIMEFRAMES[timeframe] - 1) // TIMEFRAMES[timeframe])
    for bars in (3, 16, 32):
        changed = max(bars, (parameters["level_window_minutes"] + TIMEFRAMES[timeframe] - 1)
                      // TIMEFRAMES[timeframe])
        if changed != effective:
            variants.append((f"minimum_window_{bars}", {**parameters, "minimum_window_bars": bars}))
    return variants


def run_campaign(store, frames, names, spec, cost, report_dir, capital=1000, risk_fraction=.0025,
                 start=None, end=None, timeframes=DEFAULT_TIMEFRAMES, risk_limits=None,
                 parameters_override=None, include_setup_study=True, investigate_rules=False, progress=None):
    """Run each strategy/interval independently and retain every missing-data result."""
    risk_limits = risk_limits or BacktestRiskLimits()
    overrides = parameters_override or {}
    if len(names) > 1 and overrides and any(name not in names for name in overrides):
        raise ValueError("For all strategies, --parameters-json must map strategy names to their parameter objects")
    folder = Path(report_dir) / f"batch_{utcnow():%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:8]}"
    folder.mkdir(parents=True, exist_ok=False)
    runs, coverage, comparisons = [], [], []
    for timeframe in dict.fromkeys(timeframes):
        frame = frames.get(timeframe, pd.DataFrame())
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe: {timeframe}")
        evaluated = frame
        if not evaluated.empty and start is not None:
            evaluated = evaluated.loc[evaluated.index >= start]
        if not evaluated.empty and end is not None:
            evaluated = evaluated.loc[evaluated.index < end]
        if len(evaluated) < 3:
            coverage.append({"timeframe": timeframe, "status": "NO EVALUATION HISTORY", "rows": len(evaluated)})
            continue
        first, last = evaluated.index[0], evaluated.index[-1] + pd.Timedelta(minutes=TIMEFRAMES[timeframe])
        available_days = (last - first).total_seconds() / 86400
        coverage.append({"timeframe": timeframe, "status": "AVAILABLE", "rows": len(evaluated),
                         "start": first.isoformat(), "end_exclusive": last.isoformat(),
                         "calendar_days": available_days,
                         "leading_shortfall_days": max(0, (first - start).total_seconds() / 86400) if start is not None else 0,
                         "trailing_shortfall_days": max(0, (end - last).total_seconds() / 86400) if end is not None else 0})
        for name in names:
            override = overrides.get(name, {}) if len(names) > 1 or name in overrides else overrides
            parameters = {**store.get(f"strategy_parameters:{name}", {}), **override, "entry_timeframe": timeframe}
            model = build(name, parameters)
            parameters = model.parameters.model_dump()
            variants = [("default", parameters)] + (_rule_variants(name, parameters, timeframe) if investigate_rules else [])
            paired = {}
            for variant, variant_parameters in variants:
                model = build(name, variant_parameters)
                signals = model.signals({timeframe: frame}, spec.symbol)
                modes: list[Literal["account", "setup_study"]] = ["account", "setup_study"] if variant == "default" and include_setup_study else \
                    ["account"] if variant == "default" else ["setup_study"]
                for mode in modes:
                    if progress:
                        progress(f"Running {name} / {timeframe} / {mode} / {variant}: {len(signals)} generated setups")
                    configuration = {"strategy": name, "strategy_parameters": model.parameters.model_dump(),
                                     "strategy_config_hash": model.config_hash, "strategy_rules_version": model.rules_version,
                                     "entry_timeframe": timeframe, "variant": variant, "mode": mode,
                                     "capital": capital, "risk_fraction": risk_fraction,
                                     "risk_limits": risk_limits.model_dump(), "cost": cost.model_dump(),
                                     "symbol": spec.symbol, "source": cost.source,
                                     "symbol_specification": spec.model_dump(mode="json"),
                                     "requested_start": str(start), "requested_end_exclusive": str(end)}
                    result = run_backtest({timeframe: frame}, signals, spec, cost, capital, risk_fraction,
                                          start=start, end=end, timeframe=timeframe, risk_limits=risk_limits,
                                          configuration=configuration, mode=mode)
                    report = export_report(result, folder, f"{name} / {timeframe} / {mode} / {variant}", shared_assets=True)
                    run = {key: value for key, value in result.items() if key not in ("trades", "equity", "entry_decisions")}
                    run.update(strategy=name, variant=variant, parameters=variant_parameters,
                               report=str(report.resolve()), time_segments=_segments(result["trades"], first, last))
                    runs.append(run)
                    if variant == "default":
                        paired[mode] = run
                    if progress:
                        progress(f"Finished {name} / {timeframe} / {mode} / {variant}: "
                                 f"{run['metrics']['total_trades']} fills, ${run['metrics']['net_pnl']:.2f} net P&L")
            account, study = paired["account"], paired.get("setup_study", {})
            study_metric = study.get("metrics", {})
            comparisons.append({"strategy": name, "timeframe": timeframe, "calendar_days": available_days,
                                "setups": account["candidate_signal_count"],
                                "account_trades": account["metrics"]["total_trades"],
                                "account_pnl": account["metrics"]["net_pnl"],
                                "account_profit_factor": account["metrics"]["profit_factor"],
                                "account_drawdown": account["metrics"]["maximum_drawdown"],
                                "study_trades": study_metric.get("total_trades"), "study_pnl": study_metric.get("net_pnl"),
                                "study_profit_factor": study_metric.get("profit_factor"),
                                "study_expectancy_r": study_metric.get("expectancy_r"),
                                "study_over_budget": study.get("execution_audit", {}).get("filled_over_reference_risk_budget"),
                                "account_report": account["report"], "study_report": study.get("report")})
    result = {"status": "EXPLORATORY MULTI-TIMEFRAME BACKTEST", "approved": False,
              "initial_capital": capital, "risk_fraction": risk_fraction, "risk_limits": risk_limits.model_dump(),
              "timeframes": list(timeframes), "strategies": list(names), "coverage": coverage,
              "requested_start": str(start), "requested_end_exclusive": str(end),
              "cost_review_required": not cost.costs_verified, "cost_assumptions": cost.model_dump(),
              "runs": runs, "comparisons": comparisons, "summary_report": str(folder.resolve())}
    _export_comparison(result, folder)
    with zipfile.ZipFile(folder / "batch.zip", "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in folder.rglob("*"):
            if path.is_file() and path.name != "batch.zip":
                archive.write(path, path.relative_to(folder))
    store.record("backtest_results", result)
    return result


def _export_comparison(result, folder):
    (folder / "summary.json").write_text(json.dumps(result, indent=2, default=str, allow_nan=False), encoding="utf-8")
    pd.DataFrame(result["comparisons"]).to_csv(folder / "comparisons.csv", index=False)
    sensitivity = [{"strategy": r["strategy"], "timeframe": r["entry_timeframe"], "variant": r["variant"],
                    "parameters": json.dumps(r["parameters"], sort_keys=True), **r["metrics"],
                    **{f"{segment}_{key}": value for segment, metrics in r["time_segments"].items()
                       for key, value in metrics.items()}}
                   for r in result["runs"] if r["mode"] == "setup_study"]
    pd.DataFrame(sensitivity).to_csv(folder / "sensitivity.csv", index=False)
    table = pd.DataFrame(result["comparisons"]).drop(columns=["account_report", "study_report"], errors="ignore")
    explanation = ("Account runs use the requested balance, percentage risk, broker lot rules and account guards. "
                   "Independent setup studies use the broker minimum lot on every executable setup, with overlapping "
                   "positions and account guards disabled. Over-budget fills are flagged. Study P&L is signal quality "
                   "evidence, not an achievable $1,000 account return. Rule variants are descriptive sensitivity checks; "
                   "no candidate is automatically selected or approved.")
    links = [f'<li>{html.escape(r["strategy"])} / {r["entry_timeframe"]} / {r["mode"]} / {r["variant"]}: '
             f'<a href="{Path(r["report"]).name}/report.html">report</a> · '
             f'<a href="{Path(r["report"]).name}/entry_decisions.csv">setups</a> · '
             f'<a href="{Path(r["report"]).name}/trades.csv">trades</a></li>' for r in result["runs"]]
    document = "<!doctype html><meta charset='utf-8'><h1>All-interval strategy backtest</h1><p>" \
        + explanation + "</p><h2>Coverage</h2>" + pd.DataFrame(result["coverage"]).to_html(index=False, escape=True) \
        + "<h2>Comparison</h2>" + table.to_html(index=False, escape=True) + "<h2>Run files</h2><ul>" + "".join(links) + "</ul>"
    (folder / "report.html").write_text(document, encoding="utf-8")
    lines = ["# All-interval backtest", "", explanation, "", "| Strategy | Interval | Days | Setups | Account fills | Account P&L | Study fills | Study PF | Study R |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in result["comparisons"]:
        pf = f"{row['study_profit_factor']:.3f}" if row["study_profit_factor"] is not None else "—"
        expectancy = f"{row['study_expectancy_r']:.3f}" if row["study_expectancy_r"] is not None else "—"
        lines.append(f"| {row['strategy']} | {row['timeframe']} | {row['calendar_days']:.1f} | {row['setups']} | "
                     f"{row['account_trades']} | ${row['account_pnl']:.2f} | {row['study_trades']} | {pf} | {expectancy} |")
    lines.extend(["", "See report.html for all coverage and run links; comparisons.csv and sensitivity.csv retain the numbers.",
                  "", "Cost assumptions: " + json.dumps(result["cost_assumptions"], sort_keys=True)])
    (folder / "review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
