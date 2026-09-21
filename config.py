"""
config.py -- environment loading with fail-fast validation.

Credentials are checked at startup, not at first use. A daemon that
discovers a missing key on its first trade has already spent money on
research it cannot act on.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass

log = logging.getLogger("config")


def load_dotenv_if_present(path: str = ".env") -> bool:
    """
    Load .env into os.environ if python-dotenv is installed.

    VS Code's `envFile` setting covers launches from the debugger, but not
    `python daemon.py` in a terminal. Without this the two paths behave
    differently, which is a confusing way to discover a missing key.

    Existing environment variables win, so an explicit
    `SWARM_MODE=paper python daemon.py` is never silently overridden by
    the file.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return False
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        return False
    load_dotenv(p, override=False)
    return True


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    # --- credentials -------------------------------------------------
    typesafe_api_key: str = ""
    polymarket_key_id: str = ""
    polymarket_secret_key: str = ""

    # --- models ------------------------------------------------------
    # jev-latest floats. OpenRouter says the family slug "always redirects
    # to the latest model", so pinning to jev-1.13 does NOT pin either --
    # a model update would silently recalibrate every gate threshold under
    # you. Use a versioned id (jev-1.13.0) once you have calibrated against one.
    jev_model: str = "jev-latest"
    tier2_model: str = "anthropic/claude-haiku-4-5"
    tier3_model: str = "anthropic/claude-opus-5"

    # --- mode --------------------------------------------------------
    # Default to the safe thing. Live trading must be turned on
    # deliberately, in the environment, by a human.
    mode: str = "shadow"          # shadow | paper | live

    log_level: str = "INFO"
    shadow_db: str = "shadow.db"
    ledger_db: str = "ledger.db"

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def needs_trading_credentials(self) -> bool:
        return self.mode in ("paper", "live")

    @classmethod
    def from_env(cls, dotenv: str = ".env") -> "Config":
        load_dotenv_if_present(dotenv)
        mode = os.getenv("SWARM_MODE", "shadow").strip().lower()
        if mode not in ("shadow", "paper", "live"):
            raise ConfigError(
                f"SWARM_MODE={mode!r} invalid; use shadow, paper, or live"
            )
        return cls(
            typesafe_api_key=os.getenv("TYPESAFE_API_KEY", ""),
            polymarket_key_id=os.getenv("POLYMARKET_KEY_ID", ""),
            polymarket_secret_key=os.getenv("POLYMARKET_SECRET_KEY", ""),
            jev_model=os.getenv("TYPESAFE_DEFAULT_MODEL", "jev-latest"),
            tier2_model=os.getenv("TIER2_MODEL", "anthropic/claude-haiku-4-5"),
            tier3_model=os.getenv("TIER3_MODEL", "anthropic/claude-opus-5"),
            mode=mode,
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            shadow_db=os.getenv("SHADOW_DB", "shadow.db"),
            ledger_db=os.getenv("RISK_LEDGER_DB", "ledger.db"),
        )

    def validate(self) -> list[str]:
        """Returns a list of problems. Empty means good to go."""
        problems: list[str] = []

        if not self.typesafe_api_key:
            problems.append("TYPESAFE_API_KEY is not set (needed for triage)")

        if self.needs_trading_credentials:
            if not self.polymarket_key_id:
                problems.append("POLYMARKET_KEY_ID is not set")
            if not self.polymarket_secret_key:
                problems.append("POLYMARKET_SECRET_KEY is not set")

        if self.mode == "live":
            if os.getenv("I_UNDERSTAND_THIS_TRADES_REAL_MONEY") != "yes":
                problems.append(
                    "mode=live requires I_UNDERSTAND_THIS_TRADES_REAL_MONEY=yes"
                )
            # `jev-preview` floats too, so the test is "is a versioned id",
            # not "does not end in latest".
            if not re.fullmatch(r"jev-\d+\.\d+\.\d+", self.jev_model):
                problems.append(
                    f"mode=live with an unpinned model ({self.jev_model}). "
                    "A model update would recalibrate your gate thresholds "
                    "silently. Pin a versioned id such as jev-1.13.0."
                )
        return problems

    def require(self) -> "Config":
        problems = self.validate()
        if problems:
            raise ConfigError(
                "configuration problems:\n  - " + "\n  - ".join(problems)
            )
        return self

    def describe(self) -> str:
        def mask(v: str) -> str:
            return f"set ({len(v)} chars)" if v else "MISSING"

        return "\n".join([
            f"  mode                {self.mode}",
            f"  TYPESAFE_API_KEY    {mask(self.typesafe_api_key)}",
            f"  POLYMARKET_KEY_ID   {mask(self.polymarket_key_id)}",
            f"  POLYMARKET_SECRET   {mask(self.polymarket_secret_key)}",
            f"  jev model           {self.jev_model}",
            f"  tier 2              {self.tier2_model}",
            f"  tier 3              {self.tier3_model}",
        ])


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)-8s %(message)s",
        stream=sys.stderr,
    )
