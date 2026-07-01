# src/paper_trading/store.py — SQLite schema and transaction-safe persistence
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime


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

CREATE TABLE IF NOT EXISTS paper_signals (
    signal_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    scores_json TEXT NOT NULL,
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
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

    def list_trades(self, account_id, trade_date):
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM paper_trades
                WHERE account_id = ? AND trade_date = ?
                ORDER BY ticker, action
                """,
                (account_id, trade_date),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_accounts(self):
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_accounts ORDER BY created_at, account_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def save_signals(self, account_id, signal_date, trade_date, scores_json, config_json):
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO paper_signals
                (signal_id, account_id, signal_date, trade_date, scores_json, config_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (f"signal-{account_id}-{signal_date}", account_id, signal_date, trade_date, scores_json, config_json, datetime.now().isoformat(timespec="seconds")),
            )

    def load_pending_signals(self, account_id, trade_date):
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_signals WHERE account_id = ? AND trade_date = ?",
                (account_id, trade_date),
            ).fetchall()
        return [dict(row) for row in rows]

    def is_day_processed(self, account_id, date):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM paper_daily_nav WHERE account_id = ? AND nav_date = ?",
                (account_id, date),
            ).fetchone()
        return row is not None

    def save_daily_state(self, account_id, date, cash, positions, trades, task_type='DAILY'):
        """
        原子保存每日最终状态：成交、持仓、NAV、运行记录。
        覆盖写入（确保估值价格覆盖成交价格）。
        """
        with self.connect() as conn:
            now = datetime.now().isoformat(timespec="seconds")

            # 保存成交（重复自动跳过）
            for trade in trades:
                try:
                    conn.execute(
                        """
                        INSERT INTO paper_trades (trade_id, dedupe_key, order_id, account_id, trade_date, ticker, action, shares, price, commission, source, created_at)
                        VALUES (:trade_id, :dedupe_key, :order_id, :account_id, :trade_date, :ticker, :action, :shares, :price, :commission, :source, :created_at)
                        """,
                        trade,
                    )
                except sqlite3.IntegrityError:
                    pass  # 重复成交，跳过

            # 清除该日旧持仓（确保止损清仓后持仓消失）
            conn.execute(
                "DELETE FROM paper_positions WHERE account_id = ? AND as_of_date = ?",
                (account_id, date),
            )

            # 保存持仓
            positions_value = 0.0
            for ticker, pos in positions.items():
                if pos["shares"] <= 0:
                    continue
                market_value = pos["shares"] * pos["last_price"]
                positions_value += market_value
                conn.execute(
                    """
                    INSERT INTO paper_positions (account_id, as_of_date, ticker, shares, cost_price, last_price, market_value)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (account_id, date, ticker, pos["shares"], pos["cost_price"], pos["last_price"], market_value),
                )

            # 保存 NAV（覆盖）
            nav = cash + positions_value
            conn.execute(
                """
                INSERT OR REPLACE INTO paper_daily_nav (account_id, nav_date, cash, positions_value, nav, data_date, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (account_id, date, cash, positions_value, nav, date, now),
            )

            # 保存运行记录
            conn.execute(
                """
                INSERT OR REPLACE INTO paper_runs (run_id, dedupe_key, account_id, run_date, task_type, status, data_date, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (f"run-{account_id}-{date}", f"{account_id}:{date}:{task_type}", account_id, date, task_type, "SUCCESS", date, now),
            )

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

    def execute_trades_atomic(self, account_id, trade_date, orders, prices, commission_rate=0.0003, min_commission=5.0):
        """
        原子执行订单：在同一事务中写入成交、更新持仓、更新 NAV。
        买入前检查现金（含佣金），不足时整笔拒绝。
        卖出前检查持仓数量，超卖时整笔拒绝。
        缺少可靠价格时整笔拒绝。
        返回: (executed_trades, skipped_reasons)
        """
        executed = []
        skipped = []
        now = datetime.now().isoformat(timespec="seconds")

        with self.connect() as conn:
            # 获取上一日状态
            prev_row = conn.execute(
                "SELECT MAX(nav_date) as prev_date FROM paper_daily_nav WHERE account_id = ? AND nav_date < ?",
                (account_id, trade_date),
            ).fetchone()
            prev_date = prev_row["prev_date"] if prev_row and prev_row["prev_date"] else None
            if prev_date is None:
                acct_row = conn.execute(
                    "SELECT start_date FROM paper_accounts WHERE account_id = ?", (account_id,)
                ).fetchone()
                prev_date = acct_row["start_date"] if acct_row else trade_date

            nav_row = conn.execute(
                "SELECT * FROM paper_daily_nav WHERE account_id = ? AND nav_date = ?",
                (account_id, prev_date),
            ).fetchone()
            if nav_row is None:
                raise KeyError(f"missing NAV: {account_id} {prev_date}")
            cash = nav_row["cash"]

            pos_rows = conn.execute(
                "SELECT * FROM paper_positions WHERE account_id = ? AND as_of_date = ?",
                (account_id, prev_date),
            ).fetchall()
            positions = {p["ticker"]: dict(p) for p in pos_rows}

            for order in orders:
                ticker = order["ticker"]
                action = order["action"]
                price = prices.get(ticker)

                # 缺少可靠价格：整笔拒绝
                if price is None or price <= 0:
                    skipped.append((order["order_id"], f"missing price for {ticker}"))
                    continue

                shares = abs(order["delta_shares"])
                if shares <= 0:
                    continue

                commission = max(shares * price * commission_rate, min_commission)

                # 超卖检查：整笔拒绝
                if action in ("SELL", "STOP_LOSS"):
                    available = positions.get(ticker, {}).get("shares", 0)
                    if shares > available:
                        skipped.append((order["order_id"], f"oversell: want {shares}, have {available}"))
                        continue

                # 现金不足检查：整笔拒绝
                if action == "BUY":
                    cost = shares * price + commission
                    if cash < cost:
                        skipped.append((order["order_id"], f"insufficient cash: need {cost:.2f}, have {cash:.2f}"))
                        continue
                    cash -= cost
                elif action in ("SELL", "STOP_LOSS"):
                    cash += shares * price - commission
                else:
                    continue

                # 写入成交记录（同日重复会自动跳过）
                order_id = order.get("order_id")
                trade_id = f"trade-{account_id}-{trade_date}-{ticker}-{action}-{now.replace(':', '')}"
                trade = {
                    "trade_id": trade_id,
                    "dedupe_key": f"{account_id}:{trade_date}:{ticker}:{action}",
                    "order_id": None,  # 避免外键约束问题
                    "account_id": account_id,
                    "trade_date": trade_date,
                    "ticker": ticker,
                    "action": action,
                    "shares": shares,
                    "price": price,
                    "commission": commission,
                    "source": "SIMULATED",
                    "created_at": now,
                }
                try:
                    conn.execute(
                        """
                        INSERT INTO paper_trades (trade_id, dedupe_key, order_id, account_id, trade_date, ticker, action, shares, price, commission, source, created_at)
                        VALUES (:trade_id, :dedupe_key, :order_id, :account_id, :trade_date, :ticker, :action, :shares, :price, :commission, :source, :created_at)
                        """,
                        trade,
                    )
                    executed.append(trade)
                except sqlite3.IntegrityError:
                    # 同日重复成交：回滚 cash 并跳过
                    skipped.append((order["order_id"], f"duplicate trade for {ticker}:{action}"))
                    if action == "BUY":
                        cash += shares * price + commission
                    elif action in ("SELL", "STOP_LOSS"):
                        cash -= shares * price - commission
                    continue

                # 更新持仓内存状态
                if action == "BUY":
                    if ticker in positions:
                        p = positions[ticker]
                        total_cost = p["cost_price"] * p["shares"] + price * shares
                        p["shares"] += shares
                        p["cost_price"] = total_cost / p["shares"] if p["shares"] > 0 else 0
                        p["last_price"] = price
                    else:
                        positions[ticker] = {
                            "ticker": ticker, "shares": shares,
                            "cost_price": price, "last_price": price,
                        }
                elif action in ("SELL", "STOP_LOSS"):
                    if ticker in positions:
                        p = positions[ticker]
                        p["shares"] -= shares
                        if p["shares"] <= 0:
                            del positions[ticker]

            # 清除该日旧持仓
            conn.execute(
                "DELETE FROM paper_positions WHERE account_id = ? AND as_of_date = ?",
                (account_id, trade_date),
            )

            # 写入更新后的持仓
            positions_value = 0.0
            for ticker, p in positions.items():
                market_value = p["shares"] * p["last_price"]
                positions_value += market_value
                conn.execute(
                    """
                    INSERT INTO paper_positions (account_id, as_of_date, ticker, shares, cost_price, last_price, market_value)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (account_id, trade_date, ticker, p["shares"], p["cost_price"], p["last_price"], market_value),
                )

            # 写入 NAV
            nav = cash + positions_value
            conn.execute(
                """
                INSERT OR REPLACE INTO paper_daily_nav (account_id, nav_date, cash, positions_value, nav, data_date, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (account_id, trade_date, cash, positions_value, nav, trade_date, now),
            )

        return executed, skipped

        return executed, skipped

    def insert_nav(self, row):
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO paper_daily_nav (
                    account_id, nav_date, cash, positions_value,
                    nav, data_date, created_at
                ) VALUES (
                    :account_id, :nav_date, :cash, :positions_value,
                    :nav, :data_date, :created_at
                )
                """,
                row,
            )

    def _insert_positions(self, position_rows):
        """批量插入或替换持仓（内部使用，供 runner 更新持仓）。"""
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO paper_positions (
                    account_id, as_of_date, ticker, shares,
                    cost_price, last_price, market_value
                ) VALUES (
                    :account_id, :as_of_date, :ticker, :shares,
                    :cost_price, :last_price, :market_value
                )
                """,
                position_rows,
            )

    def get_previous_nav_date(self, account_id, date):
        """获取账户最近一个记录日期。"""
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT MAX(nav_date) as prev_date
                FROM paper_daily_nav
                WHERE account_id = ? AND nav_date < ?
                """,
                (account_id, date),
            ).fetchone()
            if row and row["prev_date"]:
                return row["prev_date"]
            acct = conn.execute(
                "SELECT start_date FROM paper_accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            return acct["start_date"] if acct else None

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
