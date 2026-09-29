"""
start.py  -  ONE command to launch the whole system.
=====================================================================
RUN:  python start.py

This boots both engines and opens the single unified dashboard:

  1. StockRanker (nse_scanner.py)   - free NSE OI data -> WHICH stocks
     have high conviction. Writes conviction_bridge.json each scan.
  2. Orderflow  (orderflow_scanner.py) - Upstox depth -> WHERE to enter
     (proven absorption levels). Reads the bridge, cross-references, and
     serves the combined "CONVICTION SETUPS" dashboard.

You get ONE screen showing only high-conviction stocks that also have a
proven entry level - steady, not flickering.

PREREQUISITES (once):
  - Both projects' configs filled in.
  - For orderflow: run its `upstox_auth.py` first each morning (this
    launcher reminds you if the token is missing).

The launcher starts each engine as a child process, streams their logs
with a tag, and shuts both down cleanly on Ctrl+C.
"""

import subprocess
import sys
import time
import threading
import webbrowser
from pathlib import Path

HERE = Path(__file__).parent.resolve()

# All files live in this one folder. StockRanker and the orderflow scanner
# both sit right here, and the shared conviction_bridge.json lands here too.
STOCKRANKER = HERE / "nse_scanner.py"
ORDERFLOW   = HERE / "orderflow_scanner.py"

ORDERFLOW_PORT = 5062   # the unified dashboard opens here


def _stream(proc, tag, color):
    """Print a child process's output with a colored tag."""
    for line in iter(proc.stdout.readline, b""):
        try:
            text = line.decode(errors="replace").rstrip()
        except Exception:
            continue
        if text:
            print(f"{color}[{tag}]\033[0m {text}")


def _launch(script, tag, color, cwd):
    if not Path(script).exists():
        print(f"  [!] {tag}: script not found at {script}")
        return None
    import os
    env = dict(os.environ)
    env["COCKPIT_LAUNCH"] = "1"   # tells children to run headless
    proc = subprocess.Popen(
        [sys.executable, "-u", str(script)],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=env,
    )
    t = threading.Thread(target=_stream, args=(proc, tag, color), daemon=True)
    t.start()
    return proc


def main():
    print("\n" + "=" * 60)
    print("  LIVE TRADING COCKPIT  -  starting both engines")
    print("=" * 60)

    # token check for orderflow
    token_file = HERE / "access_token.json"
    if not token_file.exists():
        print("\n  [!] Upstox token not found.")
        print("      Run this first (once each morning):")
        print("        cd", HERE)
        print("        python upstox_auth.py")
        print("      Then run  python start.py  again.\n")
        return

    procs = []

    # 1. StockRanker (writes conviction_bridge.json into ITS folder; the
    #    orderflow tool reads from ITS own cwd, so we point the bridge there)
    print(f"\n  Starting StockRanker : {STOCKRANKER}")
    p1 = _launch(STOCKRANKER, "RANK", "\033[36m", cwd=HERE)
    if p1:
        procs.append(("StockRanker", p1))

    time.sleep(2)

    # 2. Orderflow + unified dashboard
    print(f"  Starting Orderflow   : {ORDERFLOW}")
    p2 = _launch(ORDERFLOW, "FLOW", "\033[32m", cwd=HERE)
    if p2:
        procs.append(("Orderflow", p2))

    if not procs:
        print("\n  Nothing started - check the script paths above.\n")
        return

    # open the single dashboard
    time.sleep(4)
    url = f"http://localhost:{ORDERFLOW_PORT}"
    print(f"\n  Opening the unified dashboard: {url}")
    print("  (Both engines are running. Press Ctrl+C to stop everything.)\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    # keep alive until Ctrl+C
    try:
        while True:
            # if a child dies, report it
            for name, p in procs:
                if p.poll() is not None:
                    print(f"\n  [!] {name} exited (code {p.returncode}).")
            time.sleep(2)
            if all(p.poll() is not None for _, p in procs):
                print("\n  All engines stopped.")
                break
    except KeyboardInterrupt:
        print("\n  Shutting down both engines...")
        for name, p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        time.sleep(1)
        for name, p in procs:
            try:
                if p.poll() is None:
                    p.kill()
            except Exception:
                pass
        print("  Stopped cleanly.\n")


if __name__ == "__main__":
    main()