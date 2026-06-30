# src/paper_trading/store.py — SQLite schema and transaction-safe persistence
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
