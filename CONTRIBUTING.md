# Contributing

Read [README.md](README.md) for what the project is testing and
[CLAUDE.md](CLAUDE.md) for the hard rules and the bugs already paid for.

## Get running

```bash
make setup      # or the PowerShell steps in the README
make check      # ruff + 328 tests, no network, no keys
```

Then put **your own** `TYPESAFE_API_KEY` in `.env` and run
`python verify_setup.py`. Shadow mode needs nothing else and places no orders.

## Keys and private data

- Use your own keys. Never commit `.env`, never paste a key into an issue, a
  PR, a commit message or a chat.
- `*.db` files hold collected data and are gitignored. Do not commit them.
- No personal details about any contributor or operator in code, comments,
  docs or commit messages. Say "the operator".
- Trading is per person. Nobody else's account is reachable from your clone.

## Where things go

| kind of change | where | how |
| --- | --- | --- |
| Jev question wording, thresholds, structural limits | `questions.py` | **Proposal first.** Add it to [docs/PROPOSALS.md](docs/PROPOSALS.md) with evidence, or open a PR that explains the measurement. |
| Restricted-veto wording | `questions.py` | Only ever tighten. Run `python tools/probe_restricted.py` before and after, and paste both outputs in the PR. |
| Exchange field handling | `adapters.py` | Verify against a live response, not the SDK type stubs. A fixture is not evidence. |
| Pipeline plumbing | `swarm.py`, `daemon.py`, `shadow.py` | Normal PR with tests. |
| One-off analysis | `tools/` | Read-only against the databases where possible. |

Any change to a question's wording recalibrates its thresholds. Re-measure on
a fresh shadow run before trusting a floor or `min_gate_score`.

## Rules that are not up for discussion in a PR

These are enforced by tests and listed in full in [CLAUDE.md](CLAUDE.md):
orders only from `daemon.py`; no LLM output is ever a size; the restricted
veto is never widened or averaged; `SWARM_MODE` defaults to `shadow`; the kill
switch never clears itself.

## Reporting results

Count **events**, not markets, and days when a slate shares a shock. Markets
inside one event resolve together. State the number of events and days next
to any return or calibration figure, or do not state the figure.

## Pull requests

Branch from `master`, keep `make check` green, and describe what you measured.
CI runs the same two commands.
