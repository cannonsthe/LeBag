"""
LeBag PC Launcher — runs all services in one terminal window.
All output is prefixed and colour-coded so you can tell them apart.

Usage:
    .venv\\Scripts\\python launcher.py [--pi-ip <PI_IP>]

Or just double-click start_lebag.bat which calls this automatically.
"""

import subprocess
import threading
import sys
import os
import time
import signal

# ── Colour codes (work on modern Windows terminals / PowerShell / VSCode) ─────
RESET  = "\033[0m"
BOLD   = "\033[1m"
RED    = "\033[91m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
GREY   = "\033[90m"

MAGENTA = "\033[95m"

LABELS = {
    "tele":    (GREEN,   "  NOTIFY  "),
    "server":  (YELLOW,  "  SERVER  "),
    "camera":  (CYAN,    "  CAMERA  "),
    "tracker": (MAGENTA, "  TRACKER "),
}

processes = []

# ─────────────────────────────────────────────────────────────────────────────
def stream_output(proc, label_key):
    colour, label = LABELS[label_key]
    prefix = f"{colour}{BOLD}[{label}]{RESET} "
    try:
        for raw_line in proc.stdout:
            line = raw_line.rstrip('\n').rstrip('\r')
            if line.strip():
                print(f"{prefix}{line}", flush=True)
    except Exception:
        pass

def start_service(label_key, cmd, cwd=None, extra_env=None):
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"           # Force UTF-8 for emoji in print()
    env["PYTHONIOENCODING"] = "utf-8"
    if extra_env:
        env.update(extra_env)

    colour, label = LABELS[label_key]
    print(f"{colour}{BOLD}[{label}]{RESET} Starting: {' '.join(cmd)}", flush=True)

    proc = subprocess.Popen(
        cmd,
        cwd=cwd or PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    processes.append(proc)
    t = threading.Thread(target=stream_output, args=(proc, label_key), daemon=True)
    t.start()
    return proc

def shutdown(sig=None, frame=None):
    print(f"\n{RED}{BOLD}[LAUNCHER]{RESET} Stopping all services...", flush=True)
    for p in processes:
        try:
            p.terminate()
        except Exception:
            pass
    for p in processes:
        try:
            p.wait(timeout=5)
        except Exception:
            pass
    print(f"{GREEN}{BOLD}[LAUNCHER]{RESET} All stopped. Goodbye.", flush=True)
    sys.exit(0)

# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Enable ANSI colours on Windows
    os.system("")

    PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
    VENV_PYTHON  = os.path.join(PROJECT_ROOT, ".venv", "Scripts", "python.exe")
    NOTIFICATION = os.path.join(PROJECT_ROOT, "notification")

    # Read PI_IP from args or env
    pi_ip = os.environ.get("LEBAG_PI_IP", "")
    if "--pi-ip" in sys.argv:
        idx = sys.argv.index("--pi-ip")
        if idx + 1 < len(sys.argv):
            pi_ip = sys.argv[idx + 1]

    extra_env = {}
    if pi_ip:
        extra_env["LEBAG_PI_IP"] = pi_ip
        extra_env["LEBAG_PI_STREAM_URL"] = f"tcp://{pi_ip}:5000"
        print(f"\n{BOLD}[LAUNCHER]{RESET} Pi IP: {pi_ip}  Stream: tcp://{pi_ip}:5000  NFC: http://{pi_ip}:5002")
    else:
        print(f"\n{YELLOW}{BOLD}[LAUNCHER]{RESET} ⚠️  No Pi IP set. Camera will fall back to webcam, NFC polling disabled.")
        print(f"           Set PI_IP in config.env or run: python launcher.py --pi-ip <IP>")

    print(f"{BOLD}[LAUNCHER]{RESET} Starting LeBag services...\n")

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # 1 — Notification server
    start_service("tele", ["node", "tele.js"], cwd=NOTIFICATION)
    time.sleep(2)

    # 2 — Central hub  (-u = unbuffered so every print() shows immediately)
    start_service("server", [VENV_PYTHON, "-u", "server.py"], extra_env=extra_env)
    time.sleep(2)

    # 3 — Camera reader (-u = unbuffered; cv2.imshow() still opens its own GUI window)
    start_service("camera", [VENV_PYTHON, "-u", "camera_reader.py"], extra_env=extra_env)
    time.sleep(1)

    # 4 — YOLO tracker (baggage.pt) — LEBAG_ANDROID_URL comes from the start_lebag.bat prompt
    start_service("tracker", [VENV_PYTHON, "-u", "tracker.py"], extra_env=extra_env)

    print(f"\n{BOLD}[LAUNCHER]{RESET} All services running. Press Ctrl+C to stop everything.\n")

    # Keep alive
    try:
        while True:
            # Restart any crashed service
            for p in list(processes):
                if p.poll() is not None:
                    processes.remove(p)
            time.sleep(5)
    except KeyboardInterrupt:
        shutdown()
