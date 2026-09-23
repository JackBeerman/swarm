"""
run_daily.py -- the whole shadow routine, once, safely. Places no orders.

The data only compounds if it is collected every day. This runs each step
as its own subprocess with its own timeout, keeps going when one fails,
and writes one log per day under logs/. Schedule it twice a day (see
--print-schedule); it never registers anything itself.

    python run_daily.py                 # everything
    python run_daily.py --only weather,arb
    python run_daily.py --print-schedule

Two guards learned the hard way (2026-09-22):
  * a lock file, so two runs never overlap;
  * a refusal to start while another swarm process that hammers the
    exchange is alive (an orphaned recorder shared the rate limit for a
    whole day and got the IP blocked repeatedly).
"""

from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys
import time
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parent
LOCK = ROOT / ".daily.lock"
LOG_DIR = ROOT / "logs"
WAIT_FOR_OTHERS_S = 300
EXCHANGE_HEAVY = ("fastlane.py", "shadow.py", "daemon.py", "arb.py", "inplay.py", "closer.py",
                  "weather_ensemble.py")

#: (name, argv after the interpreter, timeout seconds). Order matters:
#: fill outcomes before anything that reports on them.
STEPS: list[tuple[str, list[str], int]] = [
    ("weather-backfill", ["weather.py", "--backfill"], 600),
    ("weather", ["weather.py"], 300),
    ("ensemble-backfill", ["weather_ensemble.py", "--backfill"], 600),
    ("ensemble", ["weather_ensemble.py"], 600),
    ("arb", ["arb.py"], 900),
    ("shadow-collect", ["shadow.py", "--collect", "--limit", "200", "--events", "60",
                        "--min-hours", "1"], 2400),
    ("shadow-backfill", ["shadow.py", "--backfill"], 2400),
    ("traces-backfill", ["traces.py", "--backfill"], 900),
    ("fastlane-backfill", ["fastlane.py", "--backfill"], 900),
    ("closer-report", ["closer.py", "--report"], 300),
    ("paper", ["paper.py"], 300),
    ("dashboard-export", ["tools/dashboard_export.py"], 300),
]


def other_swarm_processes() -> list[str]:
    """Command lines of running swarm scripts that use the exchange (Windows only)."""
    if os.name != "nt":
        return []
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "ForEach-Object { $_.ProcessId.ToString() + ' ' + $_.CommandLine }"],
            capture_output=True, text=True, timeout=30).stdout
    except Exception:  # noqa: BLE001 -- a failed check must not block the run
        return []
    me = str(os.getpid())
    return [line.strip() for line in out.splitlines()
            if line.strip() and not line.startswith(me + " ")
            and any(s in line for s in EXCHANGE_HEAVY)]


def acquire_lock(max_age_h: float = 6.0) -> bool:
    """One run at a time. A lock older than max_age_h is stale and taken over."""
    if LOCK.exists() and time.time() - LOCK.stat().st_mtime < max_age_h * 3600:
        return False
    LOCK.write_text(f"{os.getpid()} {datetime.now().isoformat()}")
    return True


def select_steps(only: str | None) -> list[tuple[str, list[str], int]]:
    if not only:
        return list(STEPS)
    wanted = {s.strip() for s in only.split(",") if s.strip()}
    picked = [s for s in STEPS if s[0] in wanted or s[0].split("-")[0] in wanted]
    unknown = wanted - {s[0] for s in picked} - {s[0].split("-")[0] for s in picked}
    if unknown:
        raise SystemExit(f"unknown step(s): {', '.join(sorted(unknown))}; "
                         f"known: {', '.join(s[0] for s in STEPS)}")
    return picked


def run(steps: list[tuple[str, list[str], int]]) -> int:
    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"daily-{datetime.now():%Y%m%d}.log"
    failed = []
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n==== run at {datetime.now():%Y-%m-%d %H:%M:%S} ====\n")
        for name, argv, timeout in steps:
            t0 = time.monotonic()
            log.write(f"\n---- {name}: {' '.join(argv)}\n")
            log.flush()
            try:
                rc = subprocess.run([sys.executable, *argv], cwd=ROOT, stdout=log,
                                    stderr=subprocess.STDOUT, timeout=timeout).returncode
            except subprocess.TimeoutExpired:
                rc = "timeout"
            dt = time.monotonic() - t0
            status = "ok" if rc == 0 else f"FAILED ({rc})"
            if rc != 0:
                failed.append(name)
            line = f"{name:<20} {status:<14} {dt:6.0f}s"
            print(line, flush=True)
            log.write(f"---- {line}\n")
    print(f"log: {log_path}")
    if failed:
        print(f"failed: {', '.join(failed)}")
    return 1 if failed else 0


SCHEDULE_HELP = r"""
Register twice-daily runs with Windows Task Scheduler (run in PowerShell, once):

  $py  = "{py}"
  $dir = "{root}"
  schtasks /Create /TN "swarm-daily-am" /SC DAILY /ST 09:00 /F /TR "cmd /c cd /d $dir && $py run_daily.py"
  schtasks /Create /TN "swarm-daily-pm" /SC DAILY /ST 21:30 /F /TR "cmd /c cd /d $dir && $py run_daily.py"

Closing lines only exist if they are captured before each start:

  schtasks /Create /TN "swarm-closer" /SC MINUTE /MO 10 /F /TR "cmd /c cd /d $dir && $py closer.py --capture"

Remove with: schtasks /Delete /TN swarm-daily-am /F  (and -pm, and swarm-closer).
The machine must be awake at those times. Nothing here places orders.
The dashboard is refreshed from the export by asking Claude to push it.
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the shadow routine once. Places no orders.")
    ap.add_argument("--only", help="comma-separated step names (or prefixes, e.g. weather)")
    ap.add_argument("--force", action="store_true",
                    help="run even if another swarm process is using the exchange")
    ap.add_argument("--print-schedule", action="store_true")
    args = ap.parse_args()
    if args.print_schedule:
        print(SCHEDULE_HELP.format(py=sys.executable, root=ROOT))
        return 0
    steps = select_steps(args.only)
    # A closing-line capture pass (closer.py, every ~10 min) takes seconds:
    # wait it out rather than skip the day. Anything still running after
    # the wait is a long job or an orphan, and the run refuses.
    others = other_swarm_processes()
    waited = 0
    while others and not args.force and waited < WAIT_FOR_OTHERS_S:
        time.sleep(15)
        waited += 15
        others = other_swarm_processes()
    if others and not args.force:
        print("another swarm process is using the exchange; not starting (use --force):")
        for o in others:
            print("  " + o[:160])
        return 2
    if not acquire_lock():
        print(f"a run is already in progress ({LOCK.name}); not starting")
        return 2
    try:
        return run(steps)
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
