# Polymarket US trading swarm

Three-tier evaluation pipeline. **Defaults to shadow mode — it will not
place an order until you deliberately turn that on.**

## Layout

| file | what it is |
|---|---|
| `questions.py` | **Edit this one.** Every Jev question and gate threshold. |
| `swarm.py` | The pipeline: Jev triage → gatherers → Astra → sizing. |
| `risk_engine.py` | NLV, persistent cost ledger, kill switch. |
| `adapters.py` | Normalizes polymarket_us SDK types. Do not bypass. |
| `schemas.py` | Pydantic contracts between tiers. |
| `shadow.py` | Triage-only calibration runner. **Start here.** |
| `daemon.py` | Entry point. |
| `config.py` | Env loading with fail-fast validation. |

## Setup (VS Code)

```bash
make setup          # venv + deps + creates .env from the template
# put your Jev key in .env  ->  TYPESAFE_API_KEY=...
make test           # 84 tests, no network, no key needed
python verify_setup.py
```

No `make` on Windows? The same four steps, spelled out (PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env          # then put your Jev key in it
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe verify_setup.py
```

Then in VS Code: **Python: Select Interpreter** -> `.venv\Scripts\python.exe`
(`./.venv/bin/python` on macOS/Linux).
Tests appear in the Testing sidebar; `.vscode/launch.json` has debug
configs for each shadow command and for paper mode.

`verify_setup.py` makes exactly one live Jev call (~$0.00007) to prove
the key works *and* that the response shape matches what the code parses.

There is deliberately **no launch config and no make target for live
mode.** Live trading should be a conscious command typed in a terminal,
not something you can fat-finger from a dropdown.

## The order to do things in

**1. Calibrate the gate (days, costs cents).**

```bash
python shadow.py --collect --limit 200
python shadow.py --analyze
python shadow.py --sweep gate_score
```

Target a 3–6% escalation rate. Set `min_gate_score` in `questions.py`
from the sweep. The shipped default is tuned against *synthetic* data and
means nothing until you do this.

**2. Paper trade (weeks).**

```bash
export POLYMARKET_KEY_ID=... POLYMARKET_SECRET_KEY=...
SWARM_MODE=paper python daemon.py
```

Full pipeline, real money tracked, no orders placed. This is where you
find out whether the escalated markets are *good*, not just few.

**3. Live — only after 2 has a track record.**

```bash
export SWARM_MODE=live
export I_UNDERSTAND_THIS_TRADES_REAL_MONEY=yes
export TYPESAFE_DEFAULT_MODEL=jev-1.13-20260917   # must be pinned
python daemon.py
```

## Exit codes

| code | meaning | supervisor should |
|---|---|---|
| 0 | clean operator stop | may restart |
| 2 | **kill switch** | **not restart** |
| 3 | config problem | not restart |

A kill switch writes a `.halted` sentinel. Startup refuses while it
exists. Delete it by hand after reviewing the `halts` table in
`ledger.db` — that manual step is the point.

## Not built yet

- **`web_search` is a stub.** `_unconfigured_search` returns nothing, so
  gatherers produce empty facts. Wire a provider before paper trading.
- **`reflexion.py`** — needs a few hundred resolved markets first.
- **Resolution backfill** — `shadow.db` has a `resolved_outcome` column
  nothing fills. Until it is filled you are calibrating escalation
  *rate*, not escalation *quality*.
- **Websocket ingestion** — currently REST polling on a timer.

## Things that will bite you

- `jev-latest` and `jev-1.13` both float. Pin a dated version or your
  thresholds silently recalibrate on a model update.
- `CreateOrderParams.quantity` is an `int`. Whole shares only.
- `GetUserPositionsResponse.positions` is a **dict** keyed by slug.
- SDK prices are `Amount` objects with decimal **strings**, not floats.
- Field names are not the legacy CLOB ones: `title` not `question`,
  `volume` not `volumeNum`, `volumeMin` not `volumeNumMin`. `endTime`
  lives on the *Event*.
