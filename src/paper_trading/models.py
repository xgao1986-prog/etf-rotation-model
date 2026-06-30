# src/paper_trading/models.py — enums, immutable inputs, validation helpers, and configuration hashing
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Tuple


class AccountType(str, Enum):
    COMPARISON = "COMPARISON"
    SHADOW = "SHADOW"


class AccountStatus(str, Enum):
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    ENDED = "ENDED"
    ERROR = "ERROR"


class StartMode(str, Enum):
    CASH = "CASH"
    IMPORTED = "IMPORTED"


def canonical_config(config: Mapping[str, Any]) -> str:
    return json.dumps(
        config,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def config_hash(config: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_config(config).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class OpeningPosition:
    ticker: str
    shares: int
    cost_price: float
    last_price: float

    def __post_init__(self):
        if not self.ticker:
            raise ValueError("ticker is required")
        if self.shares <= 0 or self.shares % 100 != 0:
            raise ValueError("shares must be a positive multiple of 100")
        if self.cost_price <= 0 or self.last_price <= 0:
            raise ValueError("opening prices must be positive")

    @property
    def market_value(self) -> float:
        return self.shares * self.last_price


@dataclass(frozen=True)
class AccountCreate:
    account_id: str
    name: str
    account_type: AccountType
    strategy_name: str
    strategy_config: Mapping[str, Any]
    initial_capital: float
    start_mode: StartMode
    start_date: str
    opening_cash: float | None = None
    opening_positions: Tuple[OpeningPosition, ...] = field(default_factory=tuple)
    group_id: str | None = None
    end_date: str | None = None

    def __post_init__(self):
        if not self.account_id or not self.name or not self.strategy_name:
            raise ValueError("account_id, name, and strategy_name are required")
        if self.initial_capital <= 0:
            raise ValueError("initial capital must be positive")
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end date cannot be before start date")
        if self.start_mode is StartMode.CASH:
            if self.opening_positions:
                raise ValueError("cash start cannot contain opening positions")
            if self.opening_cash not in (None, self.initial_capital):
                raise ValueError("cash start opening cash must equal initial capital")
        if self.start_mode is StartMode.IMPORTED:
            if self.opening_cash is None or self.opening_cash < 0:
                raise ValueError("imported start requires non-negative opening cash")
