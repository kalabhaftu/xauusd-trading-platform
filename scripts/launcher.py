"""Small Windows GUI launcher for the project's local Python environment."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import StringVar, Tk, messagebox, ttk


def find_project_root() -> Path | None:
    starts = [Path(sys.executable).resolve().parent, Path.cwd()]
    if not getattr(sys, "frozen", False):
        starts.append(Path(__file__).resolve().parent.parent)
    seen: set[Path] = set()
    for start in starts:
        for candidate in (start, *start.parents):
            if candidate in seen:
                continue
            seen.add(candidate)
            if (candidate / "app" / "main.py").is_file() and (candidate / "app" / "dashboard" / "home.py").is_file():
                return candidate
    return None


def check_project(root: Path | None) -> bool:
    return bool(root and (root / ".venv" / "Scripts" / "python.exe").is_file()
                and (root / ".env").is_file())


def main() -> int:
    root = find_project_root()
    if "--self-test" in sys.argv:
        return 0 if check_project(root) else 2
    if root is None:
        messagebox.showerror("Aurum launcher", "Place AurumLauncher.exe in the project folder and try again.")
        return 2

    python = root / ".venv" / "Scripts" / "python.exe"
    url = "http://127.0.0.1:8501"
    root_logs = root / "logs"
    root_logs.mkdir(exist_ok=True)

    window = Tk()
    window.title("Aurum • XAU/USD Trading Platform")
    window.geometry("650x485")
    window.minsize(600, 450)
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
    account_text = StringVar(value=f"Project: {root}")

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
        with open(root_logs / log_name, "a", encoding="utf-8") as output:
            return subprocess.Popen([str(python), *arguments], cwd=root, stdout=output, stderr=output,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                                    close_fds=True)

    def status_snapshot() -> dict | None:
        if not python.is_file():
            return None
        try:
            result = subprocess.run([str(python), "-m", "app.main", "status"], cwd=root, capture_output=True,
                                    text=True, timeout=12, creationflags=subprocess.CREATE_NO_WINDOW
                                    if os.name == "nt" else 0)
            if result.returncode == 0:
                return json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            return None
        return None

    def refresh_status() -> None:
        def work():
            if not check_project(root):
                window.after(0, lambda: mode_text.set("Setup needed • create .env and install the locked Python environment"))
                return
            snapshot = status_snapshot()
            if not snapshot:
                window.after(0, lambda: mode_text.set("Project is ready • no service status is available yet"))
                return
            service = snapshot.get("service", {})
            account = snapshot.get("account", {})
            environment = account.get("environment", "not connected")
            label = ("LIVE ACCOUNT • REAL MONEY • losses are real" if environment == "LIVE"
                     else "DEMO ACCOUNT • simulated broker funds" if environment == "DEMO"
                     else f"Configured environment: {environment}")
            armed = snapshot.get("armed", {}).get("active", False)
            mode = snapshot.get("execution_mode", "AUTO_DEMO")
            state = service.get("state", "STOPPED")
            suffix = " • automatic order permission on" if armed else " • automatic order permission off"
            summary = f"{label}\nService: {state} • Mode: {mode}{suffix}"
            if snapshot.get("halt", {}).get("active"):
                summary += " • persistent halt active"
            window.after(0, lambda: mode_text.set(summary))
            if account:
                window.after(0, lambda: account_text.set(
                    f"Account: {account.get('server')} / {account.get('login')} • {account.get('currency', '')}"))

        threading.Thread(target=work, daemon=True).start()

    def open_dashboard() -> None:
        if not check_project(root):
            messagebox.showwarning("Setup required", "Run Setup / repair first, then configure the local .env file.")
            return
        try:
            with urllib.request.urlopen(url + "/_stcore/health", timeout=2) as response:
                if response.status == 200:
                    webbrowser.open(url)
                    status_text.set("Dashboard opened")
                    return
        except (OSError, urllib.error.URLError):
            pass
        invoke(["-m", "streamlit", "run", "app/dashboard/home.py", "--server.address", "127.0.0.1",
                "--server.port", "8501", "--browser.gatherUsageStats", "false"], "dashboard-console.log")
        status_text.set("Starting the local dashboard…")

        def wait_for_dashboard():
            for _ in range(40):
                try:
                    with urllib.request.urlopen(url + "/_stcore/health", timeout=2) as response:
                        if response.status == 200:
                            window.after(0, lambda: (webbrowser.open(url), status_text.set("Dashboard opened")))
                            return
                except (OSError, urllib.error.URLError):
                    time.sleep(0.5)
            window.after(0, lambda: status_text.set("Dashboard did not start; see logs/dashboard-console.log"))

        threading.Thread(target=wait_for_dashboard, daemon=True).start()

    def start_monitor() -> None:
        if not check_project(root):
            messagebox.showwarning("Setup required", "Run Setup / repair first, then configure the local .env file.")
            return
        snapshot = status_snapshot()
        state = snapshot.get("service", {}).get("state", "STOPPED") if snapshot else "STOPPED"
        if state not in ("STOPPED", "STALE", "FAILED"):
            messagebox.showinfo("Already running", f"Trading service status is {state}. No second service was started.")
            return
        invoke(["-m", "app.main", "demo"], "service-console.log")
        status_text.set("Monitor starting • checks all selected strategies • orders need approval and account permission")
        refresh_status()

    def stop_monitor() -> None:
        try:
            completed = subprocess.run([str(python), "-m", "app.main", "stop"], cwd=root, capture_output=True,
                                       text=True, timeout=12, creationflags=subprocess.CREATE_NO_WINDOW
                                       if os.name == "nt" else 0)
            if completed.returncode == 0:
                status_text.set("Graceful stop requested")
                refresh_status()
            else:
                messagebox.showerror("Stop failed", "See logs/service-console.log for details.")
        except (OSError, subprocess.SubprocessError):
            messagebox.showerror("Stop failed", "Could not request a graceful service stop.")

    def doctor() -> None:
        if not check_project(root):
            messagebox.showwarning("Setup required", "Run Setup / repair first, then configure the local .env file.")
            return
        status_text.set("Checking MT5 connection and market data…")

        def work():
            try:
                completed = subprocess.run([str(python), "-m", "app.main", "doctor"], cwd=root,
                                           capture_output=True, text=True, timeout=45,
                                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                (root_logs / "doctor-console.log").write_text(completed.stdout + completed.stderr,
                                                              encoding="utf-8")
                report = json.loads(completed.stdout) if completed.stdout.strip() else {}
                account = report.get("account", {})
                label = report.get("broker", "Broker check did not complete")
                if account:
                    label = (f"{account.get('environment')} account {account.get('login')} on "
                             f"{account.get('server')} • {report.get('symbol', 'symbol unavailable')}\n"
                             f"Trading permission: {account.get('trade_allowed')} • "
                             f"External API permission: {account.get('external_api_allowed')}\n"
                             "Read-only check; no orders submitted.")
                if completed.returncode:
                    label = f"Check failed • see logs/doctor-console.log\n{label}"
                window.after(0, lambda: (status_text.set("Connection check finished"),
                                         messagebox.showinfo("MT5 connection check", label)))
                refresh_status()
            except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
                error_text = str(error)
                window.after(0, lambda: (status_text.set("Connection check failed"),
                                         messagebox.showerror("MT5 connection check", error_text)))

        threading.Thread(target=work, daemon=True).start()

    def setup() -> None:
        if not (root / "scripts" / "setup.ps1").is_file():
            messagebox.showerror("Setup unavailable", "scripts/setup.ps1 was not found in the project folder.")
            return
        try:
            subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                              str(root / "scripts" / "setup.ps1")], cwd=root,
                             creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0)
            status_text.set("Setup launched in a PowerShell window")
        except OSError as error:
            messagebox.showerror("Setup failed", str(error))

    actions = [
        ("Open dashboard", open_dashboard), ("Start strategy monitor", start_monitor),
        ("Stop monitor gracefully", stop_monitor), ("Check MT5 connection", doctor),
        ("Setup / repair Python", setup), ("Open project folder", lambda: os.startfile(root)),
    ]
    for index, (label, action) in enumerate(actions):
        ttk.Button(action_grid, text=label, command=action).grid(row=index // 2, column=index % 2,
                                                                sticky="ew", padx=5, pady=5)

    ttk.Separator(container).pack(fill="x", pady=15)
    ttk.Label(container, textvariable=status_text, style="Sub.TLabel", wraplength=590).pack(anchor="w")
    ttk.Label(container,
              text="Opening the dashboard alone does not scan the market. Start the strategy monitor to check all selected rules (all three are selected by default). Auto execute is only the mode; orders still need strategy approval and your account permission.",
              style="Sub.TLabel", wraplength=590).pack(anchor="w", pady=(9, 0))

    refresh_status()
    window.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
