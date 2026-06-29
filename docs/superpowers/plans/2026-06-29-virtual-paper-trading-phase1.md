# Virtual Paper Trading Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an isolated, auditable virtual-account ledger that can create cash or imported-position accounts and persist accounts, positions, orders, trades, daily NAV, and run logs without changing B0.4.

**Architecture:** Add a small `paper_trading` package backed by a separate SQLite database. Domain validation stays outside the database layer; the store owns schema and atomic persistence, while the service owns account creation, configuration snapshots, opening-position validation, idempotent ledger writes, and reconciliation.

**Tech Stack:** Python 3, standard-library `sqlite3`, `dataclasses`, `enum`, `json`, `hashlib`, `uuid`, `argparse`, and `pytest`.

---

## Scope Boundary

This plan implements only Phase 1 of the approved design:

- virtual-account records;
- immutable configuration snapshots;
- cash-start and imported-position start;
- positions, orders, trades, daily NAV, and run logs;
- duplicate-write protection;
- account reconciliation;
- a minimal account administration command.

This plan does not implement:

- market-data download;
- daily scheduling;
- B0.4 signal generation;
- simulated T+1 execution;
- multi-strategy comparison rules;
- Streamlit pages;
- screenshot recognition;
- broker integration.

Those features require separate plans after Phase 1 passes review.

## File Map

Create:

- `src/paper_trading/__init__.py` — public exports.
- `src/paper_trading/models.py` — enums, immutable inputs, validation helpers, and configuration hashing.
- `src/paper_trading/store.py` — SQLite schema and transaction-safe persistence.
- `src/paper_trading/service.py` — account creation, opening balances, ledger writes, and reconciliation.
- `scripts/paper_account_admin.py` — create, list, and inspect accounts.
- `tests/test_paper_trading_models.py` — domain validation tests.
- `tests/test_paper_trading_store.py` — schema, persistence, and duplicate protection tests.
- `tests/test_paper_trading_service.py` — account lifecycle and reconciliation tests.
- `tests/test_paper_account_admin.py` — command-level smoke tests.

Modify:

- `docs/CHANGES.md` — record Phase 1 delivery and test evidence.
- `docs/CURRENT_STATE.md` — state that only the ledger exists; automation and UI remain pending.

Do not modify:

- `src/backtest.py`
- `src/strategy.py`
- `src/config.py`
- `src/rebalance_planner.py`
- B0.4 snapshots, reports, metrics, ETF pool, or parameters.

---

### Task 1: Domain Models and Configuration Snapshot

**Files:**

- Create: `src/paper_trading/__init__.py`
- Create: `src/paper_trading/models.py`
- Create: `tests/test_paper_trading_models.py`

- [ ] **Step 1: Write the failing model tests**

```python
# tests/test_paper_trading_models.py
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from paper_trading.models import (
    AccountCreate,
    AccountStatus,
    AccountType,
    OpeningPosition,
    StartMode,
    canonical_config,
    config_hash,
)


def test_config_hash_is_order_independent():
    left = {"weights": {"trend": 1, "confirm": 2}, "stop_loss": -0.08}
    right = {"stop_loss": -0.08, "weights": {"confirm": 2, "trend": 1}}
    assert canonical_config(left) == canonical_config(right)
    assert config_hash(left) == config_hash(right)


def test_cash_start_rejects_opening_positions():
    with pytest.raises(ValueError, match="cash start"):
        AccountCreate(
            account_id="acct-cash",
            name="B0.4 cash",
            account_type=AccountType.COMPARISON,
            strategy_name="B0.4",
            strategy_config={"max_holdings": 5},
            initial_capital=1_000_000,
            start_mode=StartMode.CASH,
            start_date="2026-06-29",
            opening_positions=(
                OpeningPosition("512400.SH", 100, 1.0, 1.0),
            ),
        )


def test_import_start_requires_cash_and_valid_lots():
    with pytest.raises(ValueError, match="multiple of 100"):
        OpeningPosition("512400.SH", 150, 1.0, 1.0)

    with pytest.raises(ValueError, match="opening cash"):
        AccountCreate(
            account_id="acct-import",
            name="Imported",
            account_type=AccountType.SHADOW,
            strategy_name="B0.4",
            strategy_config={"max_holdings": 5},
            initial_capital=1_000_000,
            start_mode=StartMode.IMPORTED,
            start_date="2026-06-29",
            opening_cash=-1,
        )


def test_account_status_values_are_stable():
    assert AccountStatus.READY.value == "READY"
    assert AccountStatus.RUNNING.value == "RUNNING"
    assert AccountStatus.PAUSED.value == "PAUSED"
    assert AccountStatus.ENDED.value == "ENDED"
    assert AccountStatus.ERROR.value == "ERROR"
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```powershell
py -m pytest tests/test_paper_trading_models.py -q
```

Expected: collection fails because `paper_trading.models` does not exist.

- [ ] **Step 3: Implement the domain models**

```python
# src/paper_trading/models.py
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
```

```python
# src/paper_trading/__init__.py
from .models import (
    AccountCreate,
    AccountStatus,
    AccountType,
    OpeningPosition,
    StartMode,
    canonical_config,
    config_hash,
)

__all__ = [
    "AccountCreate",
    "AccountStatus",
    "AccountType",
    "OpeningPosition",
    "StartMode",
    "canonical_config",
    "config_hash",
]
```

- [ ] **Step 4: Run the model tests**

Run:

```powershell
py -m pytest tests/test_paper_trading_models.py -q
```

Expected: `4 passed`.

- [ ] **Step 5: Commit Task 1**

```powershell
git add src/paper_trading/__init__.py src/paper_trading/models.py tests/test_paper_trading_models.py
git commit -m "feat(paper): add virtual account domain models"
```

---

### Task 2: SQLite Ledger Store

**Files:**

- Create: `src/paper_trading/store.py`
- Create: `tests/test_paper_trading_store.py`

- [ ] **Step 1: Write failing store tests**

```python
# tests/test_paper_trading_store.py
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from paper_trading.store import DuplicateLedgerEvent, PaperTradingStore


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d:
        yield PaperTradingStore(os.path.join(d, "paper.db"))


def seed_account(store, account_id="acct-1"):
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO paper_accounts (
                account_id, name, account_type, group_id, strategy_name,
                config_json, config_hash, initial_capital, start_mode,
                start_date, end_date, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id, "Seed", "COMPARISON", None, "B0.4",
                "{}", "seed-hash", 1_000_000, "CASH",
                "2026-06-29", None, "READY",
                "2026-06-29T16:00:00", "2026-06-29T16:00:00",
            ),
        )


def test_schema_contains_all_ledger_tables(store):
    with store.connect() as conn:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {
        "paper_accounts",
        "paper_positions",
        "paper_orders",
        "paper_trades",
        "paper_daily_nav",
        "paper_runs",
    }.issubset(names)


def test_duplicate_dedupe_key_is_rejected(store):
    seed_account(store)
    store.append_run(
        run_id="run-1",
        dedupe_key="acct-1:2026-06-29:DAILY",
        account_id="acct-1",
        run_date="2026-06-29",
        task_type="DAILY",
        status="SUCCESS",
        data_date="2026-06-29",
        error_message=None,
    )
    with pytest.raises(DuplicateLedgerEvent):
        store.append_run(
            run_id="run-2",
            dedupe_key="acct-1:2026-06-29:DAILY",
            account_id="acct-1",
            run_date="2026-06-29",
            task_type="DAILY",
            status="SUCCESS",
            data_date="2026-06-29",
            error_message=None,
        )


def test_config_snapshot_cannot_be_updated(store):
    seed_account(store)
    with pytest.raises(sqlite3.IntegrityError, match="configuration is immutable"):
        with store.connect() as conn:
            conn.execute(
                """
                UPDATE paper_accounts
                SET config_json = ?, config_hash = ?
                WHERE account_id = ?
                """,
                ('{"max_holdings":4}', "changed", "acct-1"),
            )
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```powershell
py -m pytest tests/test_paper_trading_store.py -q
```

Expected: collection fails because `paper_trading.store` does not exist.

- [ ] **Step 3: Implement the schema and store**

Create `src/paper_trading/store.py` with:

```python
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager


class DuplicateLedgerEvent(RuntimeError):
    pass


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS paper_accounts (
    account_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    account_type TEXT NOT NULL,
    group_id TEXT,
    strategy_name TEXT NOT NULL,
    config_json TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    initial_capital REAL NOT NULL CHECK(initial_capital > 0),
    start_mode TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_positions (
    account_id TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    shares INTEGER NOT NULL CHECK(shares >= 0),
    cost_price REAL NOT NULL CHECK(cost_price >= 0),
    last_price REAL NOT NULL CHECK(last_price >= 0),
    market_value REAL NOT NULL CHECK(market_value >= 0),
    PRIMARY KEY (account_id, as_of_date, ticker),
    FOREIGN KEY (account_id) REFERENCES paper_accounts(account_id)
);

CREATE TABLE IF NOT EXISTS paper_orders (
    order_id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE,
    account_id TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    action TEXT NOT NULL,
    current_shares INTEGER NOT NULL,
    target_shares INTEGER NOT NULL,
    delta_shares INTEGER NOT NULL,
    reference_price REAL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (account_id) REFERENCES paper_accounts(account_id)
);

CREATE TABLE IF NOT EXISTS paper_trades (
    trade_id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE,
    order_id TEXT,
    account_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    action TEXT NOT NULL,
    shares INTEGER NOT NULL CHECK(shares > 0),
    price REAL NOT NULL CHECK(price > 0),
    commission REAL NOT NULL CHECK(commission >= 0),
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (order_id) REFERENCES paper_orders(order_id),
    FOREIGN KEY (account_id) REFERENCES paper_accounts(account_id)
);

CREATE TABLE IF NOT EXISTS paper_daily_nav (
    account_id TEXT NOT NULL,
    nav_date TEXT NOT NULL,
    cash REAL NOT NULL CHECK(cash >= -0.01),
    positions_value REAL NOT NULL CHECK(positions_value >= 0),
    nav REAL NOT NULL CHECK(nav >= 0),
    data_date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (account_id, nav_date),
    FOREIGN KEY (account_id) REFERENCES paper_accounts(account_id)
);

CREATE TABLE IF NOT EXISTS paper_runs (
    run_id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE,
    account_id TEXT NOT NULL,
    run_date TEXT NOT NULL,
    task_type TEXT NOT NULL,
    status TEXT NOT NULL,
    data_date TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (account_id) REFERENCES paper_accounts(account_id)
);

CREATE TRIGGER IF NOT EXISTS paper_accounts_config_immutable
BEFORE UPDATE OF config_json, config_hash ON paper_accounts
BEGIN
    SELECT RAISE(ABORT, 'configuration is immutable');
END;
"""


class PaperTradingStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def append_run(
        self,
        run_id,
        dedupe_key,
        account_id,
        run_date,
        task_type,
        status,
        data_date,
        error_message,
    ):
        try:
            with self.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO paper_runs (
                        run_id, dedupe_key, account_id, run_date,
                        task_type, status, data_date, error_message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        dedupe_key,
                        account_id,
                        run_date,
                        task_type,
                        status,
                        data_date,
                        error_message,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if "dedupe_key" in str(exc):
                raise DuplicateLedgerEvent(dedupe_key) from exc
            raise
```

- [ ] **Step 4: Run the store tests**

Run:

```powershell
py -m pytest tests/test_paper_trading_store.py -q
```

Expected: `3 passed`.

- [ ] **Step 5: Commit Task 2**

```powershell
git add src/paper_trading/store.py tests/test_paper_trading_store.py
git commit -m "feat(paper): add isolated SQLite ledger"
```

---

### Task 3: Account Creation and Opening Balances

**Files:**

- Create: `src/paper_trading/service.py`
- Create: `tests/test_paper_trading_service.py`
- Modify: `src/paper_trading/__init__.py`
- Modify: `src/paper_trading/store.py`

- [ ] **Step 1: Write failing service tests**

```python
# tests/test_paper_trading_service.py
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from paper_trading.models import (
    AccountCreate,
    AccountType,
    OpeningPosition,
    StartMode,
)
from paper_trading.service import PaperTradingService
from paper_trading.store import PaperTradingStore


@pytest.fixture
def service():
    with tempfile.TemporaryDirectory() as d:
        yield PaperTradingService(
            PaperTradingStore(os.path.join(d, "paper.db"))
        )


def test_create_cash_account_writes_opening_nav(service):
    created = service.create_account(
        AccountCreate(
            account_id="acct-cash",
            name="B0.4",
            account_type=AccountType.COMPARISON,
            strategy_name="B0.4",
            strategy_config={"max_holdings": 5, "stop_loss": -0.08},
            initial_capital=1_000_000,
            start_mode=StartMode.CASH,
            start_date="2026-06-29",
        )
    )
    assert created["status"] == "READY"
    nav = service.get_nav("acct-cash", "2026-06-29")
    assert nav["cash"] == 1_000_000
    assert nav["positions_value"] == 0
    assert nav["nav"] == 1_000_000


def test_imported_account_requires_nav_identity(service):
    with pytest.raises(ValueError, match="opening NAV mismatch"):
        service.create_account(
            AccountCreate(
                account_id="acct-bad",
                name="Bad import",
                account_type=AccountType.SHADOW,
                strategy_name="B0.4",
                strategy_config={"max_holdings": 5},
                initial_capital=1_000_000,
                start_mode=StartMode.IMPORTED,
                start_date="2026-06-29",
                opening_cash=900_000,
                opening_positions=(
                    OpeningPosition("512400.SH", 100, 10.0, 10.0),
                ),
            )
        )


def test_imported_account_writes_opening_values(service):
    service.create_account(
        AccountCreate(
            account_id="acct-imported",
            name="Imported",
            account_type=AccountType.SHADOW,
            strategy_name="B0.4",
            strategy_config={"max_holdings": 5},
            initial_capital=1_000_000,
            start_mode=StartMode.IMPORTED,
            start_date="2026-06-29",
            opening_cash=900_000,
            opening_positions=(
                OpeningPosition("512400.SH", 10_000, 10.0, 10.0),
            ),
        )
    )
    nav = service.get_nav("acct-imported", "2026-06-29")
    assert nav["cash"] == 900_000
    assert nav["positions_value"] == 100_000
    assert nav["nav"] == 1_000_000


def test_account_config_is_immutable(service):
    service.create_account(
        AccountCreate(
            account_id="acct-fixed",
            name="Fixed",
            account_type=AccountType.COMPARISON,
            strategy_name="B0.4",
            strategy_config={"max_holdings": 5},
            initial_capital=1_000_000,
            start_mode=StartMode.CASH,
            start_date="2026-06-29",
        )
    )
    with pytest.raises(RuntimeError, match="immutable"):
        service.replace_config("acct-fixed", {"max_holdings": 4})
```

- [ ] **Step 2: Run the service tests and verify they fail**

Run:

```powershell
py -m pytest tests/test_paper_trading_service.py -q
```

Expected: collection fails because `paper_trading.service` does not exist.

- [ ] **Step 3: Add store methods used by the service**

Add these methods to `PaperTradingStore`:

```python
def create_account_snapshot(self, account_row, position_rows, nav_row):
    with self.connect() as conn:
        conn.execute(
            """
            INSERT INTO paper_accounts (
                account_id, name, account_type, group_id, strategy_name,
                config_json, config_hash, initial_capital, start_mode,
                start_date, end_date, status, created_at, updated_at
            ) VALUES (
                :account_id, :name, :account_type, :group_id, :strategy_name,
                :config_json, :config_hash, :initial_capital, :start_mode,
                :start_date, :end_date, :status, :created_at, :updated_at
            )
            """,
            account_row,
        )
        if position_rows:
            conn.executemany(
                """
                INSERT INTO paper_positions (
                    account_id, as_of_date, ticker, shares,
                    cost_price, last_price, market_value
                ) VALUES (
                    :account_id, :as_of_date, :ticker, :shares,
                    :cost_price, :last_price, :market_value
                )
                """,
                position_rows,
            )
        conn.execute(
            """
            INSERT INTO paper_daily_nav (
                account_id, nav_date, cash, positions_value,
                nav, data_date, created_at
            ) VALUES (
                :account_id, :nav_date, :cash, :positions_value,
                :nav, :data_date, :created_at
            )
            """,
            nav_row,
        )

def get_account(self, account_id):
    with self.connect() as conn:
        row = conn.execute(
            "SELECT * FROM paper_accounts WHERE account_id = ?",
            (account_id,),
        ).fetchone()
    return dict(row) if row else None

def insert_nav(self, row):
    with self.connect() as conn:
        conn.execute(
            """
            INSERT INTO paper_daily_nav (
                account_id, nav_date, cash, positions_value,
                nav, data_date, created_at
            ) VALUES (
                :account_id, :nav_date, :cash, :positions_value,
                :nav, :data_date, :created_at
            )
            """,
            row,
        )

def get_nav(self, account_id, nav_date):
    with self.connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM paper_daily_nav
            WHERE account_id = ? AND nav_date = ?
            """,
            (account_id, nav_date),
        ).fetchone()
    return dict(row) if row else None
```

- [ ] **Step 4: Implement account creation**

```python
# src/paper_trading/service.py
from __future__ import annotations

from datetime import datetime

from .models import (
    AccountCreate,
    AccountStatus,
    StartMode,
    canonical_config,
    config_hash,
)


class PaperTradingService:
    NAV_TOLERANCE = 0.01

    def __init__(self, store):
        self.store = store

    def create_account(self, request: AccountCreate):
        if self.store.get_account(request.account_id):
            raise ValueError(f"account already exists: {request.account_id}")

        positions_value = sum(
            position.market_value for position in request.opening_positions
        )
        opening_cash = (
            request.initial_capital
            if request.start_mode is StartMode.CASH
            else float(request.opening_cash)
        )
        opening_nav = opening_cash + positions_value
        if abs(opening_nav - request.initial_capital) > self.NAV_TOLERANCE:
            raise ValueError(
                f"opening NAV mismatch: {opening_nav:.2f} "
                f"!= {request.initial_capital:.2f}"
            )

        now = datetime.now().isoformat(timespec="seconds")
        account_row = {
            "account_id": request.account_id,
            "name": request.name,
            "account_type": request.account_type.value,
            "group_id": request.group_id,
            "strategy_name": request.strategy_name,
            "config_json": canonical_config(request.strategy_config),
            "config_hash": config_hash(request.strategy_config),
            "initial_capital": request.initial_capital,
            "start_mode": request.start_mode.value,
            "start_date": request.start_date,
            "end_date": request.end_date,
            "status": AccountStatus.READY.value,
            "created_at": now,
            "updated_at": now,
        }

        position_rows = [
            {
                "account_id": request.account_id,
                "as_of_date": request.start_date,
                "ticker": position.ticker,
                "shares": position.shares,
                "cost_price": position.cost_price,
                "last_price": position.last_price,
                "market_value": position.market_value,
            }
            for position in request.opening_positions
        ]
        nav_row = {
            "account_id": request.account_id,
            "nav_date": request.start_date,
            "cash": opening_cash,
            "positions_value": positions_value,
            "nav": opening_nav,
            "data_date": request.start_date,
            "created_at": now,
        }
        self.store.create_account_snapshot(account_row, position_rows, nav_row)
        return self.store.get_account(request.account_id)

    def get_nav(self, account_id, nav_date):
        return self.store.get_nav(account_id, nav_date)

    def replace_config(self, account_id, new_config):
        if not self.store.get_account(account_id):
            raise KeyError(account_id)
        raise RuntimeError(
            "account configuration is immutable; copy to a new account"
        )
```

Export `PaperTradingService` and `PaperTradingStore` from `src/paper_trading/__init__.py`.

- [ ] **Step 5: Run model, store, and service tests**

Run:

```powershell
py -m pytest tests/test_paper_trading_models.py tests/test_paper_trading_store.py tests/test_paper_trading_service.py -q
```

Expected: `11 passed`.

- [ ] **Step 6: Commit Task 3**

```powershell
git add src/paper_trading tests/test_paper_trading_service.py
git commit -m "feat(paper): create virtual accounts and opening balances"
```

---

### Task 4: Idempotent Orders, Trades, NAV, and Reconciliation

**Files:**

- Modify: `src/paper_trading/store.py`
- Modify: `src/paper_trading/service.py`
- Modify: `tests/test_paper_trading_service.py`
- Modify: `tests/test_paper_trading_store.py`

- [ ] **Step 1: Add failing ledger tests**

Append to `tests/test_paper_trading_service.py`:

```python
def test_append_same_order_twice_is_rejected(service):
    service.create_account(
        AccountCreate(
            account_id="acct-order",
            name="Order account",
            account_type=AccountType.COMPARISON,
            strategy_name="B0.4",
            strategy_config={"max_holdings": 5},
            initial_capital=1_000_000,
            start_mode=StartMode.CASH,
            start_date="2026-06-29",
        )
    )
    order = {
        "order_id": "order-1",
        "dedupe_key": "acct-order:2026-07-02:512400.SH:BUY",
        "account_id": "acct-order",
        "signal_date": "2026-07-02",
        "trade_date": "2026-07-03",
        "ticker": "512400.SH",
        "action": "BUY",
        "current_shares": 0,
        "target_shares": 100,
        "delta_shares": 100,
        "reference_price": 1.0,
        "reason": "B0.4 selected",
        "status": "PENDING",
    }
    service.append_order(order)
    with pytest.raises(RuntimeError, match="duplicate ledger event"):
        service.append_order(order)


def test_reconcile_detects_bad_nav(service):
    service.create_account(
        AccountCreate(
            account_id="acct-reconcile",
            name="Reconcile",
            account_type=AccountType.COMPARISON,
            strategy_name="B0.4",
            strategy_config={"max_holdings": 5},
            initial_capital=1_000_000,
            start_mode=StartMode.CASH,
            start_date="2026-06-29",
        )
    )
    assert service.reconcile("acct-reconcile", "2026-06-29")["ok"]
    service.store.insert_nav(
        {
            "account_id": "acct-reconcile",
            "nav_date": "2026-06-30",
            "cash": 900_000,
            "positions_value": 50_000,
            "nav": 1_000_000,
            "data_date": "2026-06-30",
            "created_at": "2026-06-30T16:00:00",
        }
    )
    result = service.reconcile("acct-reconcile", "2026-06-30")
    assert not result["ok"]
    assert result["difference"] == -50_000
```

Append to `tests/test_paper_trading_store.py`:

```python
def test_duplicate_trade_is_rejected(store):
    seed_account(store, "acct-trade")
    trade = {
        "trade_id": "trade-1",
        "dedupe_key": "acct-trade:2026-07-03:512400.SH:BUY",
        "order_id": None,
        "account_id": "acct-trade",
        "trade_date": "2026-07-03",
        "ticker": "512400.SH",
        "action": "BUY",
        "shares": 100,
        "price": 1.0,
        "commission": 5.0,
        "source": "SIMULATED",
        "created_at": "2026-07-03T09:30:00",
    }
    store.append_trade(trade)
    duplicate = dict(trade, trade_id="trade-2")
    with pytest.raises(DuplicateLedgerEvent):
        store.append_trade(duplicate)
```

- [ ] **Step 2: Run the new tests and verify they fail**

Run:

```powershell
py -m pytest tests/test_paper_trading_service.py -q
```

Expected: failures because `append_order` and `reconcile` do not exist.

- [ ] **Step 3: Add idempotent store writes**

Add `append_order`, `append_trade`, `list_positions`, and `list_accounts` to `PaperTradingStore`. For `append_order` and `append_trade`, catch a unique `dedupe_key` violation and raise `DuplicateLedgerEvent`, matching `append_run`.

Use this exact order insert:

```python
def append_order(self, row):
    try:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO paper_orders (
                    order_id, dedupe_key, account_id, signal_date,
                    trade_date, ticker, action, current_shares,
                    target_shares, delta_shares, reference_price,
                    reason, status, created_at
                ) VALUES (
                    :order_id, :dedupe_key, :account_id, :signal_date,
                    :trade_date, :ticker, :action, :current_shares,
                    :target_shares, :delta_shares, :reference_price,
                    :reason, :status, :created_at
                )
                """,
                row,
            )
    except sqlite3.IntegrityError as exc:
        if "dedupe_key" in str(exc):
            raise DuplicateLedgerEvent(row["dedupe_key"]) from exc
        raise
```

Use this exact trade insert:

```python
def append_trade(self, row):
    try:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO paper_trades (
                    trade_id, dedupe_key, order_id, account_id,
                    trade_date, ticker, action, shares, price,
                    commission, source, created_at
                ) VALUES (
                    :trade_id, :dedupe_key, :order_id, :account_id,
                    :trade_date, :ticker, :action, :shares, :price,
                    :commission, :source, :created_at
                )
                """,
                row,
            )
    except sqlite3.IntegrityError as exc:
        if "dedupe_key" in str(exc):
            raise DuplicateLedgerEvent(row["dedupe_key"]) from exc
        raise
```

Use these exact read methods:

```python
def list_positions(self, account_id, as_of_date):
    with self.connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM paper_positions
            WHERE account_id = ? AND as_of_date = ?
            ORDER BY ticker
            """,
            (account_id, as_of_date),
        ).fetchall()
    return [dict(row) for row in rows]

def list_accounts(self):
    with self.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM paper_accounts ORDER BY created_at, account_id"
        ).fetchall()
    return [dict(row) for row in rows]
```

- [ ] **Step 4: Add service-level duplicate handling and reconciliation**

Add to `PaperTradingService`:

```python
from .store import DuplicateLedgerEvent


def append_order(self, order):
    row = dict(order)
    row["created_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        self.store.append_order(row)
    except DuplicateLedgerEvent as exc:
        raise RuntimeError(f"duplicate ledger event: {exc}") from exc

def reconcile(self, account_id, nav_date):
    nav = self.store.get_nav(account_id, nav_date)
    if nav is None:
        raise KeyError(f"missing NAV: {account_id} {nav_date}")
    expected = nav["cash"] + nav["positions_value"]
    difference = expected - nav["nav"]
    return {
        "account_id": account_id,
        "nav_date": nav_date,
        "expected_nav": expected,
        "recorded_nav": nav["nav"],
        "difference": difference,
        "ok": abs(difference) <= self.NAV_TOLERANCE,
    }

def list_accounts(self):
    return self.store.list_accounts()

def get_account(self, account_id):
    account = self.store.get_account(account_id)
    if account is None:
        raise KeyError(account_id)
    return account
```

- [ ] **Step 5: Run all Phase 1 tests**

Run:

```powershell
py -m pytest tests/test_paper_trading_models.py tests/test_paper_trading_store.py tests/test_paper_trading_service.py -q
```

Expected: all tests pass, including duplicate order and reconciliation failures.

- [ ] **Step 6: Commit Task 4**

```powershell
git add src/paper_trading/store.py src/paper_trading/service.py tests/test_paper_trading_store.py tests/test_paper_trading_service.py
git commit -m "feat(paper): add idempotent ledger and reconciliation"
```

---

### Task 5: Minimal Account Administration Command

**Files:**

- Create: `scripts/paper_account_admin.py`
- Create: `tests/test_paper_account_admin.py`

- [ ] **Step 1: Write failing command tests**

```python
# tests/test_paper_account_admin.py
import json
import os
import subprocess
import sys
import tempfile


SCRIPT = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "paper_account_admin.py"
)


def test_create_and_list_cash_account():
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "paper.db")
        create = subprocess.run(
            [
                sys.executable,
                SCRIPT,
                "--db",
                db_path,
                "create-cash",
                "--account-id",
                "acct-cli",
                "--name",
                "CLI B0.4",
                "--strategy",
                "B0.4",
                "--initial-capital",
                "1000000",
                "--start-date",
                "2026-06-29",
                "--config-json",
                '{"max_holdings":5}',
            ],
            capture_output=True,
            text=True,
        )
        assert create.returncode == 0, create.stderr
        listed = subprocess.run(
            [sys.executable, SCRIPT, "--db", db_path, "list"],
            capture_output=True,
            text=True,
        )
        assert listed.returncode == 0, listed.stderr
        assert "acct-cli" in listed.stdout
        assert "B0.4" in listed.stdout
```

- [ ] **Step 2: Run the command test and verify it fails**

Run:

```powershell
py -m pytest tests/test_paper_account_admin.py -q
```

Expected: failure because `paper_account_admin.py` does not exist.

- [ ] **Step 3: Implement the command**

Create `scripts/paper_account_admin.py` with three commands:

- `create-cash`
- `list`
- `inspect`

The command must:

- add `src` to `sys.path`;
- default `--db` to `database/paper_trading.db`;
- parse configuration with `json.loads`;
- call `PaperTradingService`, not write SQL directly;
- print JSON for `inspect`;
- return a non-zero exit code on validation errors.

Core creation call:

```python
request = AccountCreate(
    account_id=args.account_id,
    name=args.name,
    account_type=AccountType(args.account_type),
    strategy_name=args.strategy,
    strategy_config=json.loads(args.config_json),
    initial_capital=args.initial_capital,
    start_mode=StartMode.CASH,
    start_date=args.start_date,
    end_date=args.end_date,
    group_id=args.group_id,
)
account = service.create_account(request)
print(json.dumps(account, ensure_ascii=False, sort_keys=True))
```

The `list` command must call `service.list_accounts()` and print one JSON object per account. The `inspect` command must call `service.get_account()`, `service.get_nav()`, and `service.reconcile()`, then print:

```json
{
  "account": {},
  "opening_nav": {},
  "reconciliation": {}
}
```

- [ ] **Step 4: Run command and Phase 1 tests**

Run:

```powershell
py -m pytest tests/test_paper_account_admin.py tests/test_paper_trading_models.py tests/test_paper_trading_store.py tests/test_paper_trading_service.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Run a manual isolated smoke test**

Run:

```powershell
$db = Join-Path $env:TEMP "paper_trading_smoke.db"
Remove-Item -LiteralPath $db -ErrorAction SilentlyContinue
py scripts/paper_account_admin.py --db $db create-cash --account-id smoke-b04 --name "Smoke B0.4" --strategy B0.4 --initial-capital 1000000 --start-date 2026-06-29 --config-json '{"max_holdings":5,"stop_loss":-0.08}'
py scripts/paper_account_admin.py --db $db list
py scripts/paper_account_admin.py --db $db inspect --account-id smoke-b04
Remove-Item -LiteralPath $db
```

Expected:

- create prints account `smoke-b04` with status `READY`;
- list contains exactly `smoke-b04`;
- inspect reports cash `1000000`, positions value `0`, NAV `1000000`, and reconciliation `ok=true`.

- [ ] **Step 6: Commit Task 5**

```powershell
git add scripts/paper_account_admin.py tests/test_paper_account_admin.py
git commit -m "feat(paper): add virtual account administration command"
```

---

### Task 6: Documentation and Final Verification

**Files:**

- Modify: `docs/CHANGES.md`
- Modify: `docs/CURRENT_STATE.md`

- [ ] **Step 1: Update project documentation**

Append this entry to `docs/CHANGES.md`:

```markdown
## 2026-06-29 — 虚拟实盘 Phase 1：独立账户账本

- 新增独立虚拟账户数据库，不修改 B0.4 数据库和策略代码。
- 支持现金启动和导入持仓启动。
- 保存不可变策略配置快照。
- 建立账户、持仓、订单、成交、每日净值和运行日志。
- 同日重复事件由唯一标识阻止。
- 支持现金、持仓市值和总资产勾稽。
- 本阶段不包含自动数据更新、自动信号、界面、截图识别或券商连接。
```

Update `docs/CURRENT_STATE.md` so the next unique task becomes:

```markdown
**虚拟实盘状态**：Phase 1 独立账户账本已完成并通过测试。

**下一步唯一任务**：Phase 2 单个 B0.4 虚拟账户自动运行。

尚未完成：每日行情更新闭环、周四信号、周五模拟成交、多策略并行、影子盘确认、界面和截图识别。
```

Do not copy the full implementation log into `CURRENT_STATE.md`.

- [ ] **Step 2: Run compilation**

Run:

```powershell
py -m py_compile src/paper_trading/models.py src/paper_trading/store.py src/paper_trading/service.py scripts/paper_account_admin.py
```

Expected: exit code `0`.

- [ ] **Step 3: Run focused tests**

Run:

```powershell
py -m pytest tests/test_paper_trading_models.py tests/test_paper_trading_store.py tests/test_paper_trading_service.py tests/test_paper_account_admin.py -q
```

Expected: all Phase 1 tests pass.

- [ ] **Step 4: Run existing live-assistant regression tests**

Run:

```powershell
py -m pytest tests/test_live_trading.py tests/test_live_daily_workflow.py -q
```

Expected: all existing live-assistant tests pass.

- [ ] **Step 5: Confirm B0.4 isolation**

Run:

```powershell
git diff --name-only 4301614..HEAD -- src/backtest.py src/strategy.py src/config.py src/rebalance_planner.py
```

Expected: no output.

Run:

```powershell
git diff --check 4301614..HEAD
```

Expected: no whitespace errors in the Phase 1 commit range. Pre-existing unrelated workspace changes must not be cleaned or included.

- [ ] **Step 6: Commit documentation**

```powershell
git add docs/CHANGES.md docs/CURRENT_STATE.md
git commit -m "docs: record virtual paper trading phase 1"
```

- [ ] **Step 7: Push and prepare QA evidence**

Push the current branch, then provide:

- Base SHA before Task 1;
- Target SHA after Task 6;
- exact modified-file list;
- compilation command and output;
- focused and regression test commands and outputs;
- smoke-test output;
- `git diff --check` output;
- proof that B0.4 production files were unchanged;
- local and remote HEAD equality.

QA must review the exact Base-to-Target range and rerun the focused tests using a temporary database.
