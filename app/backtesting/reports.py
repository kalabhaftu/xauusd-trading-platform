import html
import json
import math
import uuid
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from app.domain import utcnow


def export_report(result: dict, report_dir: Path, label: str, shared_assets: bool = False) -> Path:
    folder = report_dir / f"{utcnow():%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:8]}"
    folder.mkdir(parents=True, exist_ok=False)
    result["trades"].to_csv(folder / "trades.csv", index=False)
    result["equity"].to_csv(folder / "equity.csv")
    if "entry_decisions" in result:
        result["entry_decisions"].to_csv(folder / "entry_decisions.csv", index=False)
    summary = {k: v for k, v in result.items() if k not in ("trades", "equity", "entry_decisions")}
    (folder / "summary.json").write_text(json.dumps(summary, indent=2, default=str, allow_nan=False), encoding="utf-8")
    study = result.get("mode") == "setup_study"
    equity = result["equity"].CumulativePnL if study else result["equity"].Equity
    drawdown = equity.cummax().clip(lower=0) - equity if study else \
        100 * (1 - equity / equity.cummax().clip(lower=result["metrics"]["initial_capital"]))
    chart_indices = sorted(set(range(0, len(equity), max(1, math.ceil(len(equity) / 5000)))) | {len(equity) - 1})
    shown = equity.iloc[chart_indices]
    figure = make_subplots(rows=2, cols=1, shared_xaxes=True, subplot_titles=[
        "Cumulative setup P&L (USD)" if study else "Equity", "Drawdown (USD)" if study else "Drawdown (%)"])
    figure.add_trace(go.Scatter(x=shown.index, y=shown, name="Setup P&L" if study else "Backtest equity"), row=1, col=1)
    figure.add_trace(go.Scatter(x=shown.index, y=drawdown.iloc[chart_indices],
                               name="Drawdown USD" if study else "Drawdown %", fill="tozeroy"), row=2, col=1)
    figure.update_layout(template="plotly_dark", title="Independent setup study" if study else "Historical account simulation")
    if shared_assets:
        asset = report_dir / "plotly.min.js"
        if not asset.exists():
            from plotly.offline import get_plotlyjs
            asset.write_text(get_plotlyjs(), encoding="utf-8")
    chart = figure.to_html(full_html=False, include_plotlyjs="../plotly.min.js" if shared_assets else True)
    metrics = pd.DataFrame([{"Metric": key.replace("_", " ").title(),
                             "Result": str(value) if not isinstance(value, (dict, list)) else str(value)}
                            for key, value in result["metrics"].items()])
    configuration = pd.json_normalize(result.get("configuration", {}), sep=" / ").melt(
        var_name="Setting", value_name="Value") if result.get("configuration") else pd.DataFrame()
    text = f"<h1>{html.escape(label)}</h1><p>Historical simulation. No broker orders were submitted.</p>"
    if study:
        text += "<p><strong>Independent setup study: each setup uses the broker minimum lot; positions can overlap. " \
                "Account risk budgets, loss stops, position limits and daily trade limits are bypassed. " \
                "Trades above the requested risk budget are flagged. These P&amp;L totals are signal diagnostics, " \
                "not a feasible account balance or return.</strong></p>"
    audit = {"Candidate signals": result.get("candidate_signal_count", 0),
             "Simulated trades": len(result["trades"]),
             "Risk / sizing rejections": sum(result.get("risk_blocks", {}).values())}
    text += "<h2>Execution audit</h2>" + pd.DataFrame(
        [{"Measure": key, "Count": value} for key, value in audit.items()]).to_html(index=False, escape=True)
    if result.get("risk_blocks"):
        text += "<h3>Rejection reasons</h3>" + pd.DataFrame(
            [{"Reason": key, "Count": value} for key, value in result["risk_blocks"].items()]
        ).to_html(index=False, escape=True)
    if result.get("data_quality"):
        text += "<h2>Actual data coverage</h2>" + pd.DataFrame(
            [{"Measure": key.replace("_", " "), "Value": value}
             for key, value in result["data_quality"].items()]).to_html(index=False, escape=True)
    text += "<p>R is net P&amp;L divided by the all-in estimated stop risk at the actual fill, including costs. " \
            "entry_decisions.csv lists every generated setup, its rejection/fill status, the 0.25% (or configured) " \
            "risk budget, estimated minimum-lot risk, lot sizing, and minimum account capital for that stop.</p>"
    text += "<h2>Performance</h2>" + metrics.to_html(index=False, escape=True)
    if not configuration.empty:
        text += "<h2>Run settings</h2>" + configuration.to_html(index=False, escape=True)
    text += "<h2>Setup P&amp;L and drawdown</h2>" + chart if study else "<h2>Equity and drawdown</h2>" + chart
    if len(shown) < len(equity):
        text += "<p>The chart is sampled for display. The CSV preserves every candle.</p>"
    (folder / "report.html").write_text("<!doctype html><meta charset='utf-8'>" + text, encoding="utf-8")
    return folder


def export_research_report(result: dict, report_dir: Path) -> Path:
    folder = report_dir / f"research_{utcnow():%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:8]}"
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "summary.json").write_text(json.dumps(result, indent=2, default=str, allow_nan=False), encoding="utf-8")
    comparisons = [{"strategy": c["strategy"], "qualified": c["qualified"], "reason": c.get("reason"),
                    **c.get("validation", {})} for c in result["candidates"]]
    pd.DataFrame(comparisons).to_csv(folder / "comparisons.csv", index=False)
    sensitivity = [{"strategy": c["strategy"], **trial["parameters"], **trial["metrics"]}
                   for c in result["candidates"] for trial in c.get("sensitivity", [])]
    pd.DataFrame(sensitivity).to_csv(folder / "sensitivity.csv", index=False)
    title = "Historical strategy research • " + result["status"]
    text = "<!doctype html><meta charset='utf-8'><h1>" + html.escape(title) + "</h1>"
    text += "<h2>Strategy comparison</h2>" + pd.DataFrame(comparisons).to_html(index=False, escape=True)
    if result.get("baselines"):
        text += "<h2>Reference strategies</h2>" + pd.DataFrame(result["baselines"]).T.to_html(escape=True)
    text += "<h2>Evaluation period</h2>" + pd.DataFrame({"Boundary": ["Start", "Training ends",
        "Validation ends", "End"], "UTC": result.get("partitions", [])}).to_html(index=False, escape=True)
    text += "<p>Detailed per-strategy trade ledgers and sensitivity results are available as CSV downloads.</p>"
    (folder / "report.html").write_text(text, encoding="utf-8")
    return folder
