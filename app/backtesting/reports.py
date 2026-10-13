import html
import json
import uuid
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from app.domain import utcnow


def export_report(result: dict, report_dir: Path, label: str) -> Path:
    folder = report_dir / f"{utcnow():%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:8]}"
    folder.mkdir(parents=True, exist_ok=False)
    result["trades"].to_csv(folder / "trades.csv", index=False)
    result["equity"].to_csv(folder / "equity.csv")
    summary = {k: v for k, v in result.items() if k not in ("trades", "equity")}
    (folder / "summary.json").write_text(json.dumps(summary, indent=2, default=str, allow_nan=False), encoding="utf-8")
    equity = result["equity"].Equity
    drawdown = 100 * (1 - equity / equity.cummax().clip(lower=result["metrics"]["initial_capital"]))
    figure = make_subplots(rows=2, cols=1, shared_xaxes=True, subplot_titles=["Equity", "Drawdown (%)"])
    figure.add_trace(go.Scatter(x=equity.index, y=equity, name="Backtest equity"), row=1, col=1)
    figure.add_trace(go.Scatter(x=equity.index, y=drawdown, name="Drawdown %", fill="tozeroy"), row=2, col=1)
    figure.update_layout(template="plotly_dark", title="Historical simulation • no broker execution")
    chart = figure.to_html(full_html=False, include_plotlyjs=True)
    metrics = pd.DataFrame([{"Metric": key.replace("_", " ").title(),
                             "Result": str(value) if not isinstance(value, (dict, list)) else str(value)}
                            for key, value in result["metrics"].items()])
    configuration = pd.json_normalize(result.get("configuration", {}), sep=" / ").melt(
        var_name="Setting", value_name="Value") if result.get("configuration") else pd.DataFrame()
    text = f"<h1>{html.escape(label)}</h1><p>Historical simulation. No broker orders were submitted.</p>"
    audit = {"Candidate signals": result.get("candidate_signal_count", 0),
             "Simulated trades": len(result["trades"]),
             "Risk / sizing rejections": sum(result.get("risk_blocks", {}).values())}
    text += "<h2>Execution audit</h2>" + pd.DataFrame(
        [{"Measure": key, "Count": value} for key, value in audit.items()]).to_html(index=False, escape=True)
    if result.get("risk_blocks"):
        text += "<h3>Rejection reasons</h3>" + pd.DataFrame(
            [{"Reason": key, "Count": value} for key, value in result["risk_blocks"].items()]
        ).to_html(index=False, escape=True)
    text += "<h2>Performance</h2>" + metrics.to_html(index=False, escape=True)
    if not configuration.empty:
        text += "<h2>Run settings</h2>" + configuration.to_html(index=False, escape=True)
    text += "<h2>Equity and drawdown</h2>" + chart
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
