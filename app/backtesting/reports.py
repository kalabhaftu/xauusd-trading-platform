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
    text = f"<h1>{html.escape(label)}</h1><p>Historical simulation. Execution and financing assumptions are explicit.</p>"
    text += chart + "<pre>" + html.escape(json.dumps(summary, indent=2, default=str)) + "</pre>"
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
    text += pd.DataFrame(comparisons).to_html(index=False, escape=True)
    text += "<pre>" + html.escape(json.dumps(result, indent=2, default=str)) + "</pre>"
    (folder / "report.html").write_text(text, encoding="utf-8")
    return folder
