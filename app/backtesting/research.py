import logging
import statistics
from datetime import timedelta
from typing import Any

import pandas as pd

from app.backtesting.engine import BacktestRiskLimits, run_backtest
from app.backtesting.reports import export_report, export_research_report
from app.config.research import ResearchPolicy
from app.data.validation import TIMEFRAMES, validate
from app.domain import Direction, Signal, digest
from app.indicators.core import atr, ema
from app.strategies.registry import build

logger = logging.getLogger(__name__)


def partitions(frame, timeframe="M1", evaluation_start=None):
    if len(frame) < 1000:
        raise ValueError("Insufficient history for chronological research partitions")
    end = frame.index[-1] + pd.Timedelta(minutes=TIMEFRAMES[timeframe])
    start_index = frame.index.searchsorted(pd.Timestamp(evaluation_start)) if evaluation_start is not None else 0
    if evaluation_start is not None and start_index >= len(frame) - 1000:
        raise ValueError("Insufficient evaluation history after the requested start date")
    start = frame.index[start_index]
    span = end - start
    return start, start + span * 0.6, start + span * 0.8, end


def walk_forward_windows(start, end, policy=None):
    policy = policy or ResearchPolicy()
    train_start = start
    while True:
        test_start = train_start + pd.DateOffset(months=policy.training_months)
        test_end = test_start + pd.DateOffset(months=policy.forward_months)
        if test_end > end:
            break
        yield train_start, test_start, test_end
        train_start += pd.DateOffset(months=policy.forward_months)


def parameter_grid():
    # Stops and targets now come from each rule's price structure. Do not perform
    # the old generic ATR/2R sweep against signals that were not designed for it.
    return [{}]


def apply_stop_target_parameters(templates: list[Signal], strategy) -> list[Signal]:
    """Preserve the signal's structure-derived bracket for a research candidate."""
    return [signal.model_copy(update={"config_hash": strategy.config_hash}) for signal in templates]


def optimize(frames, name, spec, cost, capital, start, end, base=None, policy=None, risk_fraction=.0025,
             risk_limits=None, tune_stop_targets=True, signal_templates=None):
    policy = policy or ResearchPolicy()
    results = []
    grid = parameter_grid() if tune_stop_targets else [{}]
    if signal_templates is None:
        template_strategy = build(name, dict(base or {}))
        signal_templates = template_strategy.signals(
            {tf: f.loc[f.index < end] for tf, f in frames.items()}, spec.symbol)
    for variation in grid:
        parameters = {**(base or {}), **variation}
        strategy = build(name, parameters)
        generated = apply_stop_target_parameters(signal_templates, strategy)
        result = run_backtest(frames, generated, spec, cost, capital, risk_fraction, start=start, end=end,
                              timeframe=strategy.parameters.entry_timeframe, risk_limits=risk_limits)
        metric = result["metrics"]
        # Robust screening precedes ranking; no win-rate objective.
        score = metric["expectancy_r"] if metric["total_trades"] >= policy.min_training_trades \
            and metric["maximum_drawdown"] < policy.max_drawdown else None
        results.append({"parameters": parameters, "score": score, "metrics": metric})
    eligible = [r for r in results if r["score"] is not None]
    winner = max(eligible, key=lambda r: (r["score"], -r["metrics"]["maximum_drawdown"])) if eligible else None
    return winner, results


def baseline_signals(frames, symbol, name, start=None, timeframe="M1"):
    frame = frames[timeframe]
    minutes = TIMEFRAMES[timeframe]
    volatility = atr(frame)
    fast, slow = ema(frame.close, 50), ema(frame.close, 200)
    output = []
    first = max(200, frame.index.searchsorted(start) - 1) if start is not None else 200
    for i in range(first, len(frame) - 1):
        direction = None
        if name == "buy_hold" and i == first:
            direction = Direction.LONG
        if name == "ema_crossover":
            if fast.iloc[i] > slow.iloc[i] and fast.iloc[i - 1] <= slow.iloc[i - 1]:
                direction = Direction.LONG
            elif fast.iloc[i] < slow.iloc[i] and fast.iloc[i - 1] >= slow.iloc[i - 1]:
                direction = Direction.SHORT
        if direction:
            sign = 1 if direction == Direction.LONG else -1
            distance = float(volatility.iloc[i]) * (100 if name == "buy_hold" else 1.5)
            price = float(frame.close.iloc[i])
            stop = max(0.01, price - sign * distance)
            timestamp = (frame.index[i] + pd.Timedelta(minutes=minutes)).to_pydatetime()
            output.append(Signal(symbol=symbol, strategy=name, config_hash=digest(name), direction=direction,
                                 timestamp=timestamp, entry=price, stop_loss=stop,
                                 take_profit=price + sign * distance * 2, rationale="Simple reference baseline",
                                 regime="baseline", indicators={"atr": float(volatility.iloc[i])},
                                 expires_at=timestamp + timedelta(minutes=minutes)))
    return output


def research(store, frames, names, spec, cost, report_dir, capital=10000, consume_holdout=False,
             policy=None, risk_fraction=.0025, evaluation_start=None, risk_limits=None, base_parameters=None,
             tune_stop_targets=True):
    if consume_holdout and not cost.costs_verified:
        raise ValueError("Verify broker commission, slippage, and financing in Getting Started before using the holdout")
    policy = policy or ResearchPolicy()
    risk_limits = risk_limits or BacktestRiskLimits()
    entry_timeframe = next(tf for tf in TIMEFRAMES if tf in frames and not frames[tf].empty)
    base = {**(base_parameters or {}), "entry_timeframe": entry_timeframe}
    candidate_bases = {
        name: {**store.get(f"strategy_parameters:{name}", {}), **base}
        for name in names
    }
    start, validation_start, holdout_start, end = partitions(
        frames[entry_timeframe], entry_timeframe, evaluation_start)
    dataset_hash = digest({tf: f.to_json(date_format="iso") for tf, f in frames.items()})
    campaign_key = digest({"dataset": dataset_hash, "source": cost.source, "cost": cost,
                           "spec": spec.model_dump(exclude={"timestamp"}), "start": str(start), "end": str(end),
                           "policy": policy.model_dump(), "risk_fraction": risk_fraction,
                           "risk_limits": risk_limits.model_dump(), "parameters": candidate_bases,
                           "tune_stop_targets": tune_stop_targets})
    consumed_key = f"holdout_consumed:{dataset_hash}"
    if consume_holdout and store.get(consumed_key):
        raise ValueError("Dataset holdout already consumed; do not retune against previously inspected holdout")
    candidates = []
    for name in names:
        template_parameters = dict(candidate_bases[name])
        template_strategy = build(name, template_parameters)
        development_frames = {tf: f.loc[f.index < holdout_start] for tf, f in frames.items()}
        signal_templates = template_strategy.signals(development_frames, spec.symbol)
        logger.info("Research %s: generated %d development signal templates", name, len(signal_templates))
        winner, sensitivity = optimize(frames, name, spec, cost, capital, start, validation_start,
                                       base=template_parameters, policy=policy, risk_fraction=risk_fraction,
                                       risk_limits=risk_limits, tune_stop_targets=tune_stop_targets,
                                       signal_templates=signal_templates)
        if winner is None:
            candidates.append({"strategy": name, "qualified": False, "reason": "No training candidate passed screening",
                               "sensitivity": sensitivity})
            continue
        selected = build(name, winner["parameters"])
        candidate_config = {"strategy": name, "strategy_parameters": selected.parameters.model_dump(),
                            "strategy_config_hash": selected.config_hash, "risk_fraction": risk_fraction,
                            "risk_limits": risk_limits.model_dump(), "cost": cost.model_dump(),
                            "capital": capital, "entry_timeframe": entry_timeframe, "symbol": spec.symbol,
                            "source": cost.source, "symbol_specification": spec.model_dump(mode="json"),
                            "evaluation_start": str(validation_start), "evaluation_end_exclusive": str(holdout_start),
                            "tune_stop_targets": tune_stop_targets}
        validation = run_backtest(frames, apply_stop_target_parameters(signal_templates, selected),
            spec, cost, capital, risk_fraction, start=validation_start, end=holdout_start,
            timeframe=entry_timeframe, risk_limits=risk_limits, configuration=candidate_config)
        folds: list[dict[str, Any]] = []
        fold_windows = list(walk_forward_windows(start, holdout_start, policy))
        for fold_index, (train_start, test_start, test_end) in enumerate(fold_windows, start=1):
            logger.info("Research %s: optimizing forward fold %d/%d", name, fold_index, len(fold_windows))
            fold_winner, _ = optimize(frames, name, spec, cost, capital, train_start, test_start,
                                      base=candidate_bases[name], policy=policy, risk_fraction=risk_fraction,
                                      risk_limits=risk_limits, tune_stop_targets=tune_stop_targets,
                                      signal_templates=signal_templates)
            if fold_winner is None:
                folds.append({"expectancy_r": None, "expectancy": None})
                continue
            fold_strategy = build(name, fold_winner["parameters"])
            fold = run_backtest(frames, apply_stop_target_parameters(signal_templates, fold_strategy),
                spec, cost, capital, risk_fraction, start=test_start, end=test_end,
                timeframe=entry_timeframe, risk_limits=risk_limits)
            folds.append(fold["metrics"])
        scores = [float(f["expectancy_r"]) for f in folds if f["expectancy_r"] is not None]
        positive_folds = sum(1 for f in folds if (f.get("expectancy") or 0) > 0)
        metric = validation["metrics"]
        qualified = bool(metric["expectancy"] and metric["expectancy"] > 0 and metric["profit_factor"]
                         and metric["profit_factor"] > policy.min_profit_factor
                         and metric["maximum_drawdown"] < policy.max_drawdown
                         and len(folds) >= policy.min_forward_folds
                         and positive_folds / len(folds) >= policy.positive_fold_fraction)
        folder = export_report(validation, report_dir, f"{name} development validation")
        candidates.append({"strategy": name, "parameters": selected.parameters.model_dump(),
                           "config_hash": selected.config_hash, "qualified": qualified,
                           "validation": metric, "folds": folds, "sensitivity": sensitivity,
                           "median_forward_r": statistics.median(scores) if scores else None,
                           "configuration": candidate_config, "report": str(folder.resolve())})
    eligible = [c for c in candidates if c["qualified"]]
    selected = max(eligible, key=lambda c: (c["median_forward_r"], -c["validation"]["maximum_drawdown"])) \
        if eligible else None
    result = {"campaign": campaign_key, "dataset_hash": dataset_hash, "source": cost.source,
              "symbol": spec.symbol, "symbol_specification": spec.model_dump(mode="json"),
              "candidates": candidates,
              "selected": selected, "approved": False, "status": "NO STRATEGY APPROVED FOR AUTOMATION",
              "partitions": [str(x) for x in (start, validation_start, holdout_start, end)],
              "requested_evaluation_start": str(evaluation_start) if evaluation_start is not None else None,
              "available_history_start": frames[entry_timeframe].index[0].isoformat(),
              "available_history_end": frames[entry_timeframe].index[-1].isoformat(),
              "requested_history_shortfall_days": max(0, (frames[entry_timeframe].index[0]
                  - pd.Timestamp(evaluation_start)).total_seconds() / 86400)
                  if evaluation_start is not None and pd.Timestamp(evaluation_start) < frames[entry_timeframe].index[0]
                  else 0,
              "policy": policy.model_dump(), "risk_fraction": risk_fraction,
              "risk_limits": risk_limits.model_dump(), "parameters": candidate_bases,
              "cost_assumptions": cost.model_dump(), "initial_capital": capital,
              "cost_review_required": not cost.costs_verified,
              "entry_timeframe": entry_timeframe,
              "tune_stop_targets": tune_stop_targets}
    baseline_results = {}
    for baseline in ("buy_hold", "ema_crossover"):
        baseline_results[baseline] = run_backtest(frames, baseline_signals(frames, spec.symbol, baseline,
                                                                         validation_start, entry_timeframe),
                                                 spec, cost, capital, risk_fraction, start=validation_start,
                                                 end=holdout_start, timeframe=entry_timeframe,
                                                 risk_limits=risk_limits)["metrics"]
    result["baselines"] = baseline_results
    if selected and consume_holdout:
        # Atomic claim before any holdout access; crashes must not silently reopen the holdout.
        from sqlalchemy.dialects.sqlite import insert

        from app.database.models import state
        with store.engine.begin() as conn:
            claimed = conn.execute(insert(state).values(key=consumed_key,
                                   value={"campaign": campaign_key, "selected": selected["config_hash"]})
                                   .on_conflict_do_nothing())
            if claimed.rowcount != 1:
                raise ValueError("Holdout consumption already claimed")
        strategy = build(selected["strategy"], selected["parameters"])
        # Candidate and fold selection above never inspected the holdout. Claim it atomically first.
        holdout_templates = strategy.signals(frames, spec.symbol)
        generated = apply_stop_target_parameters(holdout_templates, strategy)
        holdout_config = {"strategy": strategy.name, "strategy_parameters": strategy.parameters.model_dump(),
                          "strategy_config_hash": strategy.config_hash, "risk_fraction": risk_fraction,
                          "risk_limits": risk_limits.model_dump(), "cost": cost.model_dump(),
                          "capital": capital, "entry_timeframe": entry_timeframe,
                          "evaluation_start": str(holdout_start),
                          "evaluation_end_exclusive": str(end), "tune_stop_targets": tune_stop_targets}
        test = run_backtest(frames, generated, spec, cost, capital, risk_fraction, start=holdout_start, end=end,
                            timeframe=entry_timeframe, risk_limits=risk_limits, configuration=holdout_config)
        stresses = [run_backtest(frames, generated, spec, cost.stressed(x), capital, risk_fraction,
                                start=holdout_start, end=end, timeframe=entry_timeframe,
                                risk_limits=risk_limits)["metrics"] for x in (1.5, 2)]
        metric = test["metrics"]
        oos_trades = selected["validation"]["total_trades"] + metric["total_trades"]
        quality = [validate(f, tf) for tf, f in frames.items()]
        confirmed = store.get(f"coverage_confirmed:{dataset_hash}", False)
        passed = bool(cost.costs_verified and metric["expectancy"] and metric["expectancy"] > 0 and metric["profit_factor"]
                      and metric["profit_factor"] > policy.min_profit_factor
                      and metric["maximum_drawdown"] < policy.max_drawdown
                      and oos_trades >= policy.min_oos_trades
                      and all(s["expectancy"] and s["expectancy"] > 0 for s in stresses)
                      and (confirmed or all(q["complete"] for q in quality))
                      and (metric["largest_win_share"] or 1) < policy.max_single_win_share)
        result.update(approved=passed, holdout=metric, stresses=stresses, out_of_sample_trades=oos_trades,
                      coverage_verified=confirmed or all(q["complete"] for q in quality),
                      holdout_report=str(export_report(test, report_dir, "Untouched holdout evaluation").resolve()))
        if passed:
            result["status"] = "APPROVED FOR AUTOMATED EXECUTION REVIEW; ACCOUNT PERMISSION STILL REQUIRED"
        approval = {"approved": passed, "config_hash": strategy.config_hash, "strategy": strategy.name,
                    "source": store.get("research_source"), "symbol": spec.symbol, "campaign": campaign_key,
                    "cost_hash": digest(cost), "spec_hash": digest(spec.model_dump(exclude={"timestamp"})),
                    "cost": cost.model_dump(),
                    "risk_fraction": risk_fraction, "policy": policy.model_dump(),
                    "report": result["holdout_report"]}
        store.set(f"approval:{strategy.config_hash}", approval)
        store.record("approvals", approval)
    store.record("experiments", {"campaign": campaign_key, "dataset": dataset_hash, "cost": cost.model_dump(),
                                 "spec": spec.model_dump(mode="json"), "capital": capital})
    result["summary_report"] = str(export_research_report(result, report_dir).resolve())
    store.record("backtest_results", result)
    return result
