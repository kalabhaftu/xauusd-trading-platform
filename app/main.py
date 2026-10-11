import argparse
import json
import sys
from datetime import timedelta

from app.config.settings import Settings
from app.database.storage import Store
from app.domain import SymbolSpec, digest, utcnow
from app.monitoring.health import service_health
from app.monitoring.logger import configure


def configured(store=None):
    settings = Settings()
    if store:
        values = settings.model_dump()
        values.update(store.get("settings_override", {}))
        values.update(store.get("broker_override", {}))
        values["active_strategy"] = store.get("selected_strategy", settings.active_strategy)
        settings = Settings(**values)
    return settings


def research_inputs(settings, store, args):
    from app.backtesting.engine import CostProfile
    from app.data.downloader import research_frames
    source = args.source or settings.account_server or store.get("research_source")
    symbol = args.symbol or settings.symbol or store.get("active_symbol")
    if not source or not symbol:
        raise ValueError("Specify --source and --symbol or configure the verified broker")
    from app.data.validation import TIMEFRAMES
    frames = research_frames(store, source, symbol, args.timeframe, args.trend_timeframe)
    if any(frame.empty for frame in frames.values()):
        raise ValueError("Download the selected entry/trend broker timeframes before research")
    end = pd_timestamp(args.end) if args.end else frames[args.timeframe].index[-1] \
        + timedelta(minutes=TIMEFRAMES[args.timeframe])
    start = pd_timestamp(args.start) if args.start else end - timedelta(days=365 * 3)
    # Preserve warm-up before requested start, with evaluation beginning at the requested start.
    frames = {tf: f.loc[(f.index >= start) & (f.index < end)] for tf, f in frames.items()}
    spec_data = store.get(f"spec:{source}:{symbol}")
    if not spec_data:
        raise ValueError("No broker specification snapshot; download-data/doctor must retrieve it")
    settings.require_costs()
    spread = args.spread if args.spread is not None else settings.max_spread
    if spread is None:
        raise ValueError("Explicit --spread assumption or XAU_MAX_SPREAD is required")
    account = store.get("active_account", {})
    cost = CostProfile(commission_per_lot=settings.commission_per_lot, spread_price=spread,
                       slippage_price=settings.slippage_price,
                       swap_long_per_lot_day=settings.swap_long_per_lot_day,
                       swap_short_per_lot_day=settings.swap_short_per_lot_day,
                       account_currency=account.get("currency", "USD"), margin_fraction=args.margin,
                       source=f"{source}:explicit cost profile")
    store.set("research_source", source)
    store.set("research_cost", cost.model_dump())
    return frames, SymbolSpec(**spec_data), cost


def pd_timestamp(value):
    import pandas as pd
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def doctor(settings, store):
    import importlib.util
    checks = {"database": "ok", "mode": settings.mode, "entries": "disarmed unless audited permission exists",
              "execution_tolerances": {"max_spread_price": settings.max_spread,
                                       "max_slippage_points": settings.max_slippage_points,
                                       "modeled_slippage_price": settings.slippage_price},
              "dependencies": {name: importlib.util.find_spec(name) is not None for name in
                               ("MetaTrader5", "backtesting", "streamlit", "pandas", "sqlalchemy")}}
    from sqlalchemy import text
    with store.engine.connect() as conn:
        checks["sqlite_integrity"] = conn.execute(text("PRAGMA integrity_check")).scalar()
    from app.broker.mt5_client import MT5Broker
    broker = MT5Broker(settings)
    try:
        broker.connect()
        account = broker.account()
        symbol = broker.discover()
        spec = broker.spec(symbol)
        now = utcnow()
        frame = broker.candles(symbol, "M15", now - timedelta(days=3), now)
        checks.update(account={"login": account.login, "server": account.server, "environment": account.environment},
                      symbol=symbol, history_rows=len(frame), quote=broker.quote(symbol).model_dump(mode="json"))
        checks["symbol_specification"] = spec.model_dump(mode="json")
        checks["max_slippage_price"] = (settings.max_slippage_points or 0) * spec.point
        store.set("active_account", account)
        store.set("active_symbol", symbol)
        store.set(f"spec:{account.server}:{symbol}", spec)
        checks["broker"] = "verified DEMO; no orders submitted"
    except Exception as exc:
        checks["broker"] = f"unavailable: {exc}"
    finally:
        broker.shutdown()
    return checks


def arm_demo(settings, store, args):
    from app.broker.mt5_client import MT5Broker, verify_account
    from app.data.sessions import SessionSchedule
    from app.execution.reconciliation import reconcile
    from app.risk.circuit_breaker import CircuitBreaker
    from app.service import AccountMutex, permission_fingerprint
    from app.strategies.registry import build
    settings.require_demo()
    settings.require_costs()
    if args.confirm_account != settings.account_login or not args.confirm_first_order:
        raise ValueError("Explicit account confirmation and --confirm-first-order authorization required")
    if settings.max_spread is None or settings.max_slippage_points is None:
        raise ValueError("Configure execution tolerances before arming")
    # Arming requires a stopped engine: no second process may interfere with MT5 terminal state.
    with AccountMutex(f"{settings.account_server}:{settings.account_login}"):
        broker = MT5Broker(settings)
        try:
            broker.connect()
            account = broker.account()
            verify_account(account, settings)
            if account.currency != "USD":
                raise ValueError("Initial approved research/execution supports USD demo accounts only")
            strategy = build(settings.active_strategy, store.get(f"strategy_parameters:{settings.active_strategy}", {}))
            approval = store.get(f"approval:{strategy.config_hash}", {})
            symbol = broker.discover()
            spec = broker.spec(symbol)
            cost = approval.get("cost", {})
            if not approval.get("approved") or approval.get("source") != account.server \
                    or approval.get("symbol") != symbol or approval.get("spec_hash") != digest(
                        spec.model_dump(exclude={"timestamp"})):
                raise ValueError("No matching approved strategy/specification evidence")
            if approval.get("cost_hash") != digest(cost):
                raise ValueError("Research cost profile changed since approval")
            if approval.get("risk_fraction") != settings.risk_per_trade:
                raise ValueError("Runtime risk fraction differs from approved research")
            if settings.max_spread > cost["spread_price"] or settings.slippage_price > cost["slippage_price"] \
                    or settings.commission_per_lot > cost["commission_per_lot"] \
                    or settings.max_slippage_points * spec.point > cost["slippage_price"] \
                    or settings.swap_long_per_lot_day < cost["swap_long_per_lot_day"] \
                    or settings.swap_short_per_lot_day < cost["swap_short_per_lot_day"]:
                raise ValueError("Execution tolerances exceed validated transaction-cost assumptions")
            state = reconcile(broker, store, settings)
            breaker = CircuitBreaker(store, account.key)
            if not state.safe or state.positions or breaker.current()["active"]:
                raise ValueError("Arming requires reconciled flat account and no persistent halt")
            from datetime import datetime

            from app.risk.limits import EquityBook, consecutive_losses, period_keys
            now = utcnow()
            history_start = store.get(f"history_start:{account.key}")
            if not history_start:
                raise ValueError("Disarmed monitoring must establish trustworthy account history first")
            deals = broker.history(datetime.fromisoformat(history_start), now)
            risk = EquityBook(store, account.key).observe(account, deals, now)
            reset_at = store.get(f"loss_reset:{account.key}", 0)
            risk["consecutive_losses"] = consecutive_losses(
                [d for d in deals if d.get("time", 0) > reset_at], settings.magic)
            store.set(f"risk:{account.key}", risk)
            day, week = period_keys(now)
            if not risk.get("baselines_verified") or risk.get("day") != day or risk.get("week") != week:
                raise ValueError("Current daily and weekly baselines have not been verified")
            for key, threshold in (("daily_loss", settings.daily_loss_limit), ("weekly_loss", settings.weekly_loss_limit),
                                   ("drawdown", settings.drawdown_limit),
                                   ("consecutive_losses", settings.consecutive_loss_limit)):
                if risk.get(key, threshold) >= threshold:
                    raise ValueError(f"Risk limit blocks arming: {key}")
            quote = broker.quote(symbol)
            if not 0 <= (utcnow() - quote.timestamp).total_seconds() <= settings.quote_max_age_seconds \
                    or quote.spread > settings.max_spread:
                raise ValueError("Fresh quote within spread tolerance required")
            if not SessionSchedule(settings.session_file, symbol, account.server).is_open(utcnow()):
                raise ValueError("Arming requires an open verified trading session")
            permission = {"active": True, "config_hash": strategy.config_hash,
                          "fingerprint": permission_fingerprint(settings), "authorized_at": utcnow().isoformat(),
                          "first_demo_order_explicitly_authorized": True}
            with store.engine.begin() as conn:
                store.set(f"armed:{account.key}", permission, conn)
                store.record("activity", {"event": "explicit_demo_authorization", **permission}, account.key, conn=conn)
            return {"armed": True, "strategy": strategy.name, "account": account.key,
                    "message": "No order submitted by arming; next service start may execute approved signals"}
        finally:
            broker.shutdown()


def parser():
    result = argparse.ArgumentParser(description="XAU/USD research and guarded demo automation")
    sub = result.add_subparsers(dest="command", required=True)
    for name in ("doctor", "status", "demo", "stop", "halt", "reset-halt"):
        sub.add_parser(name)
    download = sub.add_parser("download-data")
    download.add_argument("--timeframes", nargs="+", default=["M15", "H1"])
    download.add_argument("--start")
    download.add_argument("--end")
    download.add_argument("--refresh", action="store_true", help="Retry cached chunks with broker history gaps")
    for name in ("backtest", "compare-strategies", "validate-strategy"):
        research = sub.add_parser(name)
        research.add_argument("--strategy", default="trend_pullback")
        research.add_argument("--source")
        research.add_argument("--symbol")
        research.add_argument("--start")
        research.add_argument("--end")
        research.add_argument("--capital", type=float, default=10000)
        research.add_argument("--spread", type=float)
        research.add_argument("--margin", type=float, default=.01)
        research.add_argument("--policy", default="configs/research.json")
        research.add_argument("--timeframe", choices=["M5", "M15", "M30"], default="M15")
        research.add_argument("--trend-timeframe", choices=["H1", "H4"], default="H1")
        if name == "validate-strategy":
            research.add_argument("--consume-holdout", action="store_true")
    arm = sub.add_parser("arm-demo")
    arm.add_argument("--confirm-account", type=int, required=True)
    arm.add_argument("--confirm-first-order", action="store_true")
    coverage = sub.add_parser("confirm-coverage")
    coverage.add_argument("--dataset-hash", required=True)
    coverage.add_argument("--evidence", required=True, help="Path to inspected closure/gap evidence document")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        initial = Settings()
        configure(initial)
        store = Store(initial.database_url)
        store.migrate()
        settings = configured(store)
        output = None
        if args.command == "doctor":
            output = doctor(settings, store)
        elif args.command == "status":
            key = f"{settings.account_server}:{settings.account_login}"
            output = {"service": service_health(store), "account": store.get("active_account"),
                      "halt": store.get(f"halt:{key}"), "armed": store.get(f"armed:{key}", {"active": False})}
        elif args.command in ("stop", "halt", "reset-halt"):
            if args.command == "halt":
                from app.risk.circuit_breaker import CircuitBreaker
                CircuitBreaker(store, f"{settings.account_server}:{settings.account_login}").trip("Operator CLI halt")
            output = {"command_id": store.enqueue(args.command,
                       {"account": f"{settings.account_server}:{settings.account_login}"}), "state": "queued"}
        elif args.command == "demo":
            from app.service import DemoService
            DemoService(settings, store).run()
        elif args.command == "arm-demo":
            output = arm_demo(settings, store, args)
        elif args.command == "download-data":
            from app.broker.mt5_client import MT5Broker
            from app.data.downloader import download
            from app.service import AccountMutex
            with AccountMutex(f"{settings.account_server}:{settings.account_login}"):
                broker = MT5Broker(settings)
                try:
                    broker.connect()
                    symbol = broker.discover()
                    store.set("active_account", broker.account())
                    store.set("active_symbol", symbol)
                    end = pd_timestamp(args.end).to_pydatetime() if args.end else utcnow()
                    start = pd_timestamp(args.start).to_pydatetime() if args.start else end - timedelta(days=365 * 3)
                    output = [download(broker, store, symbol, tf, start, end, args.refresh) for tf in args.timeframes]
                finally:
                    broker.shutdown()
        elif args.command == "confirm-coverage":
            from pathlib import Path
            evidence = Path(args.evidence).resolve()
            if not evidence.is_file() or len(args.dataset_hash) != 64:
                raise ValueError("Existing evidence document and complete dataset hash required")
            store.set(f"coverage_confirmed:{args.dataset_hash}",
                      {"evidence": str(evidence), "evidence_hash": digest(evidence.read_text(encoding="utf-8")),
                       "confirmed_at": utcnow().isoformat()})
            output = {"coverage_confirmed": args.dataset_hash}
        else:
            frames, spec, cost = research_inputs(settings, store, args)
            from app.backtesting.reports import export_report
            from app.strategies.registry import REGISTRY, build
            if args.command == "backtest":
                from app.backtesting.engine import run_backtest
                parameters = {**store.get(f"strategy_parameters:{args.strategy}", {}),
                              "entry_timeframe": args.timeframe, "trend_timeframe": args.trend_timeframe}
                strategy = build(args.strategy, parameters)
                dataset_hash = digest({tf: f.to_json(date_format="iso") for tf, f in frames.items()})
                store.set(f"holdout_consumed:{dataset_hash}", {"reason": "Exploratory full-range backtest inspected data"})
                result = run_backtest(frames, strategy.signals(frames, spec.symbol), spec, cost,
                                      args.capital, settings.risk_per_trade, timeframe=args.timeframe)
                folder = export_report(result, settings.report_dir, args.strategy)
                output = {"metrics": result["metrics"], "dataset_hash": result["dataset_hash"],
                          "report": str(folder.resolve()), "approval": "exploratory run only"}
                store.record("backtest_results", output)
            else:
                from pathlib import Path

                from app.backtesting.research import research
                from app.config.research import ResearchPolicy
                policy = ResearchPolicy.model_validate_json(Path(args.policy).read_text(encoding="utf-8"))
                names = list(REGISTRY) if args.command == "compare-strategies" else [args.strategy]
                output = research(store, frames, names, spec, cost, settings.report_dir, args.capital,
                                  getattr(args, "consume_holdout", False), policy, settings.risk_per_trade)
        if output is not None:
            print(json.dumps(output, indent=2, default=str, allow_nan=False))
        return 0
    except Exception as exc:
        # Error strings can contain broker details, but never echo raw secret configuration.
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
