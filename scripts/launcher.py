"""Windows launcher and embedded worker entry point for Aurum."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import StringVar, Tk, messagebox, ttk


def is_packaged() -> bool:
    return bool(getattr(sys, "frozen", False))


def runtime_root() -> Path:
    if is_packaged():
        return Path(getattr(sys, "_MEIPASS")).resolve()
    return Path(__file__).resolve().parents[1]


def user_data_root() -> Path:
    if not is_packaged():
        return runtime_root()
    local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return local_app_data / "Aurum"


def configure_packaged_paths() -> tuple[Path, Path]:
    """Keep user data outside the bundled app and any protected install folder."""
    root = runtime_root()
    data = user_data_root()
    if is_packaged():
        for folder in (data, data / "data", data / "reports", data / "logs", data / "tools"):
            folder.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("AURUM_SETTINGS_FILE", str(data / "settings.env"))
        database = (data / "data" / "platform.db").as_posix()
        os.environ.setdefault("XAU_DATABASE_URL", f"sqlite:///{database}")
        os.environ.setdefault("XAU_REPORT_DIR", str(data / "reports"))
        os.environ.setdefault("XAU_SESSION_FILE", str(data / "data" / "xau_sessions.json"))
        for name in ("ExportSessions.mq5", "SessionExporterEA.mq5"):
            source = root / "scripts" / name
            destination = data / "tools" / name
            if source.is_file() and not destination.exists():
                shutil.copy2(source, destination)
    return root, data


def check_project(root: Path | None) -> bool:
    if root is None or not (root / "app" / "main.py").is_file():
        return False
    if is_packaged():
        return all((root / path).is_file() for path in (
            "app/dashboard/home.py", "configs/research.json", "alembic.ini", "migrations/env.py"))
    return (root / ".venv" / "Scripts" / "python.exe").is_file()


def _decode_status(output: str) -> dict | None:
    decoder = json.JSONDecoder()
    offset = 0
    while (start := output.find("{", offset)) >= 0:
        try:
            value, _ = decoder.raw_decode(output[start:])
        except json.JSONDecodeError:
            offset = start + 1
            continue
        if isinstance(value, dict) and "service" in value and "account" in value:
            return value
        offset = start + 1
    return None


def _worker(arguments: list[str]) -> int:
    root, _ = configure_packaged_paths()
    if arguments == ["dashboard"]:
        from streamlit.web import cli

        dashboard = root / "app" / "dashboard" / "home.py"
        sys.argv = ["streamlit", "run", str(dashboard), "--server.address", "127.0.0.1",
                    "--server.port", "8501", "--server.headless", "true",
                    "--browser.gatherUsageStats", "false"]
        try:
            cli.main()
        except SystemExit as error:
            return int(error.code or 0)
        return 0
    if len(arguments) >= 3 and arguments[:2] == ["-m", "app.main"]:
        from app.main import main as application_main

        return int(application_main(arguments[2:]) or 0)
    return 2


def _command(root: Path, data: Path, arguments: list[str]) -> list[str]:
    if is_packaged():
        return [sys.executable, "--worker", *arguments]
    return [str(root / ".venv" / "Scripts" / "python.exe"), *arguments]


def _worker_environment(packaged: bool) -> dict[str, str]:
    environment = os.environ.copy()
    if packaged:
        # Long-lived workers must extract their own one-file runtime so they survive
        # closing the launcher that started them.
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return environment


def main() -> int:
    root, data = configure_packaged_paths()
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        return _worker(sys.argv[2:])
    if "--self-test" in sys.argv:
        if not check_project(root):
            return 2
        if is_packaged():
            try:
                from streamlit.web import cli  # noqa: F401

                import app.main  # noqa: F401
            except Exception:
                return 3
        return 0
    if not check_project(root):
        messagebox.showerror("Aurum", "The application files are incomplete. Reinstall Aurum or contact its publisher.")
        return 2

    packaged = is_packaged()
    working_dir = data if packaged else root
    url = "http://127.0.0.1:8501"
    logs = data / "logs"
    logs.mkdir(parents=True, exist_ok=True)

    def app_command(*arguments: str) -> list[str]:
        return _command(root, data, ["-m", "app.main", *arguments])

    def dashboard_command() -> list[str]:
        if packaged:
            return _command(root, data, ["dashboard"])
        return _command(root, data, ["-m", "streamlit", "run", "app/dashboard/home.py",
                                     "--server.address", "127.0.0.1", "--server.port", "8501",
                                     "--browser.gatherUsageStats", "false"])

    window = Tk()
    window.title("Aurum • XAU/USD Trading Platform")
    window.geometry("640x450")
    window.minsize(590, 420)
    window.configure(bg="#111722")
    style = ttk.Style(window)
    style.theme_use("clam")
    style.configure("TFrame", background="#111722")
    style.configure("TLabel", background="#111722", foreground="#e8edf4", font=("Segoe UI", 10))
    style.configure("Title.TLabel", font=("Segoe UI Semibold", 22), foreground="#f2c879")
    style.configure("Sub.TLabel", foreground="#aab7c7")
    style.configure("TButton", font=("Segoe UI Semibold", 10), padding=(12, 9))

    mode_text = StringVar(value="Checking account and service state…")
    status_text = StringVar(value="Ready")
    account_text = StringVar(value=f"App data: {data}")

    container = ttk.Frame(window, padding=22)
    container.pack(fill="both", expand=True)
    ttk.Label(container, text="AURUM", style="Title.TLabel").pack(anchor="w")
    ttk.Label(container, text="XAU/USD research and trading", style="Sub.TLabel").pack(anchor="w", pady=(1, 17))

    state_box = ttk.Frame(container, padding=13)
    state_box.pack(fill="x", pady=(0, 15))
    ttk.Label(state_box, textvariable=mode_text, wraplength=580).pack(anchor="w")
    ttk.Label(state_box, textvariable=account_text, style="Sub.TLabel", wraplength=580).pack(anchor="w", pady=(5, 0))

    action_grid = ttk.Frame(container)
    action_grid.pack(fill="x")
    for col in range(2):
        action_grid.columnconfigure(col, weight=1)

    def invoke(arguments: list[str], log_name: str) -> subprocess.Popen:
        with (logs / log_name).open("a", encoding="utf-8") as output:
            return subprocess.Popen(arguments, cwd=working_dir, env=_worker_environment(packaged),
                                    stdout=output, stderr=output,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                                    close_fds=True)

    def status_snapshot() -> dict | None:
        try:
            result = subprocess.run(app_command("status"), cwd=working_dir, env=os.environ.copy(), capture_output=True,
                                    text=True, timeout=15,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if result.returncode == 0:
                return _decode_status(result.stdout + "\n" + result.stderr)
        except (OSError, subprocess.SubprocessError):
            return None
        return None

    def refresh_status() -> None:
        def work():
            snapshot = status_snapshot()
            if not snapshot:
                window.after(0, lambda: mode_text.set("Ready • finish the account setup in Getting Started."))
                return
            service = snapshot.get("service", {})
            account = snapshot.get("account") or {}
            environment = account.get("environment", "not connected")
            label = ("LIVE ACCOUNT • REAL MONEY" if environment == "LIVE"
                     else "DEMO ACCOUNT • simulated funds" if environment == "DEMO"
                     else "Account not connected yet")
            armed = snapshot.get("armed", {}).get("active", False)
            state = service.get("state", "STOPPED")
            suffix = " • order permission on" if armed else " • order permission off"
            summary = f"{label}\nMonitor: {state}{suffix}"
            if snapshot.get("halt", {}).get("active"):
                summary += " • safety halt active"
            window.after(0, lambda: mode_text.set(summary))
            if account:
                window.after(0, lambda: account_text.set(
                    f"Account: {account.get('server')} / {account.get('login')} • {account.get('currency', '')}"))

        threading.Thread(target=work, daemon=True).start()

    def open_dashboard() -> None:
        try:
            with urllib.request.urlopen(url + "/_stcore/health", timeout=2) as response:
                if response.status == 200:
                    webbrowser.open(url)
                    status_text.set("Dashboard opened")
                    return
        except (OSError, urllib.error.URLError):
            pass
        invoke(dashboard_command(), "dashboard.log")
        status_text.set("Starting dashboard…")

        def wait_for_dashboard():
            for _ in range(40):
                try:
                    with urllib.request.urlopen(url + "/_stcore/health", timeout=2) as response:
                        if response.status == 200:
                            window.after(0, lambda: (webbrowser.open(url), status_text.set("Dashboard opened")))
                            return
                except (OSError, urllib.error.URLError):
                    time.sleep(0.5)
            window.after(0, lambda: status_text.set("Dashboard could not start. Reopen Aurum and try again."))

        threading.Thread(target=wait_for_dashboard, daemon=True).start()

    def start_monitor() -> None:
        snapshot = status_snapshot()
        state = snapshot.get("service", {}).get("state", "STOPPED") if snapshot else "STOPPED"
        if state not in ("STOPPED", "STALE", "FAILED"):
            messagebox.showinfo("Already running", f"The market monitor is {state}. No second monitor was started.")
            return
        if not snapshot or not snapshot.get("account"):
            messagebox.showinfo("Finish setup first", "Open the dashboard and complete Getting Started before monitoring.")
            open_dashboard()
            return
        invoke(app_command("demo"), "monitor.log")
        status_text.set("Monitor starting • checks all selected strategies")
        refresh_status()

    def stop_monitor() -> None:
        try:
            result = subprocess.run(app_command("stop"), cwd=working_dir, env=os.environ.copy(), capture_output=True,
                                    text=True, timeout=15,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if result.returncode == 0:
                status_text.set("Graceful stop requested")
                refresh_status()
            else:
                messagebox.showerror("Stop failed", "Reopen Aurum and try the stop action again.")
        except (OSError, subprocess.SubprocessError):
            messagebox.showerror("Stop failed", "Could not request a graceful stop.")

    def doctor() -> None:
        status_text.set("Checking the MT5 connection…")

        def work():
            try:
                result = subprocess.run(app_command("doctor"), cwd=working_dir, env=os.environ.copy(), capture_output=True,
                                        text=True, timeout=60,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                (logs / "connection-check.log").write_text(result.stdout + result.stderr, encoding="utf-8")
                report = _decode_status(result.stdout) if '"service"' in result.stdout else None
                if not report:
                    from json import JSONDecoder
                    decoder = JSONDecoder()
                    report = None
                    for start in range(len(result.stdout)):
                        if result.stdout[start] != "{":
                            continue
                        try:
                            value, _ = decoder.raw_decode(result.stdout[start:])
                        except json.JSONDecodeError:
                            continue
                        if isinstance(value, dict) and "broker" in value:
                            report = value
                            break
                account = (report or {}).get("account", {})
                if account:
                    message = (f"Connected to {account.get('environment')} account on {account.get('server')}.\n"
                               f"Gold symbol: {report.get('symbol', 'not found')}\nRead-only check; no order was placed.")
                else:
                    message = (report or {}).get("broker", "Connection check did not complete.")
                if result.returncode:
                    message = "Check needs attention. Open Getting Started to review the account details.\n" + message
                window.after(0, lambda: (status_text.set("Connection check finished"),
                                         messagebox.showinfo("MT5 connection check", message)))
                refresh_status()
            except (OSError, subprocess.SubprocessError) as error:
                error_text = str(error)
                window.after(0, lambda: (status_text.set("Connection check failed"),
                                         messagebox.showerror("MT5 connection check", error_text)))

        threading.Thread(target=work, daemon=True).start()

    actions = [
        ("Open dashboard", open_dashboard), ("Start strategy monitor", start_monitor),
        ("Stop monitor gracefully", stop_monitor), ("Check MT5 connection", doctor),
        ("Open app data folder", lambda: os.startfile(data)),
    ]
    if not packaged:
        actions.append(("Open developer project", lambda: os.startfile(root)))
    for index, (label, action) in enumerate(actions):
        ttk.Button(action_grid, text=label, command=action).grid(row=index // 2, column=index % 2,
                                                                sticky="ew", padx=5, pady=5)

    ttk.Separator(container).pack(fill="x", pady=15)
    ttk.Label(container, textvariable=status_text, style="Sub.TLabel", wraplength=590).pack(anchor="w")
    ttk.Label(container,
              text="Open the dashboard for setup and results. Start the monitor to scan the selected strategies. "
                   "Auto execute is a preference; orders still require research approval and separate account authorization.",
              style="Sub.TLabel", wraplength=590).pack(anchor="w", pady=(9, 0))

    refresh_status()
    window.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
