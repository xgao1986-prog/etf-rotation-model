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
    is_hidden INTEGER NOT NULL DEFAULT 0 CHECK(is_hidden IN (0, 1)),
    is_deleted INTEGER NOT NULL DEFAULT 0 CHECK(is_deleted IN (0, 1)),
    closed_at TEXT,
    deleted_at TEXT,
    lifecycle_reason TEXT,
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

CREATE TABLE IF NOT EXISTS paper_skipped (
    skipped_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    order_id TEXT,
    ticker TEXT,
    action TEXT,
    reason TEXT NOT NULL,
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
    CURRENT_SCHEMA_VERSION = 3

    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    def _migrate(self, conn):
        """升级 Phase 1 旧数据库到当前 schema。"""
        # 确保 schema_version 元数据表存在
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_schema_version (
                version INTEGER PRIMARY KEY,
                updated_at TEXT NOT NULL
            )
            """
        )
        row = conn.execute(
            "SELECT version FROM paper_schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
        current = row["version"] if row else 1

        if current >= self.CURRENT_SCHEMA_VERSION:
            return

        now = datetime.now().isoformat(timespec="seconds")

        # Phase 1 -> Phase 2：添加 paper_signals, paper_skipped；重建 paper_trades 以恢复 FK
        if current < 2:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_signals (
                    signal_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    signal_date TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    scores_json TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (account_id) REFERENCES paper_accounts(account_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_skipped (
                    skipped_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    order_id TEXT,
                    ticker TEXT,
                    action TEXT,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (account_id) REFERENCES paper_accounts(account_id)
                )
                """
            )
            # 重建 paper_trades 以恢复 order_id 外键（兼容旧数据 order_id=NULL）
            conn.execute(
                """
                CREATE TABLE paper_trades_new (
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
                )
                """
            )
            conn.execute(
                """
                INSERT INTO paper_trades_new
                SELECT trade_id, dedupe_key, order_id, account_id, trade_date,
                       ticker, action, shares, price, commission, source, created_at
                FROM paper_trades
                """
            )
            conn.execute("DROP TABLE paper_trades")
            conn.execute("ALTER TABLE paper_trades_new RENAME TO paper_trades")

        # Phase 2 -> Phase 3：添加账户生命周期字段（幂等：先检查列是否存在）
        if current < 3:
            existing_cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(paper_accounts)").fetchall()
            }
            if "is_hidden" not in existing_cols:
                conn.execute(
                    """
                    ALTER TABLE paper_accounts
                    ADD COLUMN is_hidden INTEGER NOT NULL DEFAULT 0 CHECK(is_hidden IN (0, 1))
                    """
                )
            if "is_deleted" not in existing_cols:
                conn.execute(
                    """
                    ALTER TABLE paper_accounts
                    ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0 CHECK(is_deleted IN (0, 1))
                    """
                )
            if "closed_at" not in existing_cols:
                conn.execute(
                    """
                    ALTER TABLE paper_accounts
                    ADD COLUMN closed_at TEXT
                    """
                )
            if "deleted_at" not in existing_cols:
                conn.execute(
                    """
                    ALTER TABLE paper_accounts
                    ADD COLUMN deleted_at TEXT
                    """
                )
            if "lifecycle_reason" not in existing_cols:
                conn.execute(
                    """
                    ALTER TABLE paper_accounts
                    ADD COLUMN lifecycle_reason TEXT
                    """
                )

        conn.execute(
            """
            INSERT OR REPLACE INTO paper_schema_version (version, updated_at)
            VALUES (?, ?)
            """,
            (self.CURRENT_SCHEMA_VERSION, now),
        )

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

    def list_trades(self, account_id, trade_date=None):
        query = "SELECT * FROM paper_trades WHERE account_id = ?"
        params = [account_id]
        if trade_date is not None:
            query += " AND trade_date = ?"
            params.append(trade_date)
        query += " ORDER BY trade_date, ticker, action"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def list_accounts(self, include_hidden=False, include_deleted=False):
        query = "SELECT * FROM paper_accounts WHERE 1=1"
        params = []
        if not include_deleted:
            query += " AND is_deleted = 0"
        if not include_hidden:
            query += " AND is_hidden = 0"
        query += " ORDER BY created_at, account_id"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
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

    def save_daily_state(self, account_id, date, cash, positions, trades, skipped=None,
                         orders=None, signals=None, task_type='DAILY'):
        """
        原子保存每日最终状态：订单、成交、持仓、NAV、运行记录、未执行原因、信号。
        一天的所有计算完成后，最后通过本方法一次性写入；中途不得单独写入账户状态。
        失败时整个事务回滚，不会留下半套记录。
        """
        skipped = skipped or []
        orders = orders or []
        signals = signals or []
        with self.connect() as conn:
            now = datetime.now().isoformat(timespec="seconds")

            # 先删除该日旧数据，确保最终状态唯一
            conn.execute(
                "DELETE FROM paper_trades WHERE account_id = ? AND trade_date = ?",
                (account_id, date),
            )
            conn.execute(
                "DELETE FROM paper_orders WHERE account_id = ? AND trade_date = ?",
                (account_id, date),
            )
            conn.execute(
                "DELETE FROM paper_positions WHERE account_id = ? AND as_of_date = ?",
                (account_id, date),
            )
            conn.execute(
                "DELETE FROM paper_daily_nav WHERE account_id = ? AND nav_date = ?",
                (account_id, date),
            )
            conn.execute(
                "DELETE FROM paper_runs WHERE account_id = ? AND run_date = ? AND task_type = ?",
                (account_id, date, task_type),
            )
            conn.execute(
                "DELETE FROM paper_skipped WHERE account_id = ? AND trade_date = ?",
                (account_id, date),
            )

            # 保存订单（必须在成交之前，因为成交外键依赖订单）
            for order in orders:
                conn.execute(
                    """
                    INSERT INTO paper_orders (
                        order_id, dedupe_key, account_id, signal_date, trade_date,
                        ticker, action, current_shares, target_shares, delta_shares,
                        reference_price, reason, status, created_at
                    ) VALUES (
                        :order_id, :dedupe_key, :account_id, :signal_date, :trade_date,
                        :ticker, :action, :current_shares, :target_shares, :delta_shares,
                        :reference_price, :reason, :status, :created_at
                    )
                    """,
                    order,
                )

            # 保存成交
            for trade in trades:
                conn.execute(
                    """
                    INSERT INTO paper_trades (trade_id, dedupe_key, order_id, account_id, trade_date, ticker, action, shares, price, commission, source, created_at)
                    VALUES (:trade_id, :dedupe_key, :order_id, :account_id, :trade_date, :ticker, :action, :shares, :price, :commission, :source, :created_at)
                    """,
                    trade,
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

            # 保存 NAV
            nav = cash + positions_value
            conn.execute(
                """
                INSERT INTO paper_daily_nav (account_id, nav_date, cash, positions_value, nav, data_date, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (account_id, date, cash, positions_value, nav, date, now),
            )

            # 保存调仓信号（与当天状态一起成功或失败）
            for signal in signals:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO paper_signals (
                        signal_id, account_id, signal_date, trade_date, scores_json, config_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"signal-{signal['account_id']}-{signal['signal_date']}",
                        signal["account_id"],
                        signal["signal_date"],
                        signal["trade_date"],
                        signal["scores_json"],
                        signal["config_json"],
                        now,
                    ),
                )

            # 保存未执行原因
            for item in skipped:
                if isinstance(item, dict):
                    order_id = item.get("order_id")
                    ticker = item.get("ticker")
                    action = item.get("action")
                    reason = item.get("reason")
                else:
                    # backward-compat tuple/list (order_id, reason)
                    order_id = item[0] if len(item) > 0 else None
                    reason = item[1] if len(item) > 1 else None
                    ticker = None
                    action = None
                skipped_id = f"skip-{account_id}-{date}-{order_id or 'na'}-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
                conn.execute(
                    """
                    INSERT INTO paper_skipped (skipped_id, account_id, trade_date, order_id, ticker, action, reason, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (skipped_id, account_id, date, order_id, ticker, action, reason, now),
                )

            # 保存运行记录（最后一步：只有前面全部成功才会到达这里）
            conn.execute(
                """
                INSERT INTO paper_runs (run_id, dedupe_key, account_id, run_date, task_type, status, data_date, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (f"run-{account_id}-{date}", f"{account_id}:{date}:{task_type}", account_id, date, task_type, "SUCCESS", date, now),
            )

    def create_account_snapshot(self, account_row, position_rows, nav_row, conn=None):
        # 确保生命周期字段有默认值
        account_row = dict(account_row)
        account_row.setdefault("is_hidden", 0)
        account_row.setdefault("is_deleted", 0)
        account_row.setdefault("closed_at", None)
        account_row.setdefault("deleted_at", None)
        account_row.setdefault("lifecycle_reason", None)

        def _do_insert(_conn):
            _conn.execute(
                """
                INSERT INTO paper_accounts (
                    account_id, name, account_type, group_id, strategy_name,
                    config_json, config_hash, initial_capital, start_mode,
                    start_date, end_date, status, is_hidden, is_deleted,
                    closed_at, deleted_at, lifecycle_reason, created_at, updated_at
                ) VALUES (
                    :account_id, :name, :account_type, :group_id, :strategy_name,
                    :config_json, :config_hash, :initial_capital, :start_mode,
                    :start_date, :end_date, :status, :is_hidden, :is_deleted,
                    :closed_at, :deleted_at, :lifecycle_reason, :created_at, :updated_at
                )
                """,
                account_row,
            )
            if position_rows:
                _conn.executemany(
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
            _conn.execute(
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

        if conn is None:
            with self.connect() as conn:
                _do_insert(conn)
        else:
            _do_insert(conn)

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

    def list_skipped(self, account_id, trade_date):
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM paper_skipped
                WHERE account_id = ? AND trade_date = ?
                ORDER BY order_id, ticker
                """,
                (account_id, trade_date),
            ).fetchall()
        return [dict(row) for row in rows]

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

    def get_account(self, account_id):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM paper_accounts WHERE account_id = ?",
                (account_id,),
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

    def list_orders(self, account_id, status=None, start_date=None, end_date=None):
        query = "SELECT * FROM paper_orders WHERE account_id = ?"
        params = [account_id]
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        if start_date is not None:
            query += " AND trade_date >= ?"
            params.append(start_date)
        if end_date is not None:
            query += " AND trade_date <= ?"
            params.append(end_date)
        query += " ORDER BY trade_date, ticker"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_order(self, order_id):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM paper_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_order_status(self, order_id, status, reason=None):
        with self.connect() as conn:
            if reason is not None:
                conn.execute(
                    "UPDATE paper_orders SET status = ?, reason = ? WHERE order_id = ?",
                    (status, reason, order_id),
                )
            else:
                conn.execute(
                    "UPDATE paper_orders SET status = ? WHERE order_id = ?",
                    (status, order_id),
                )

    def expire_pending_orders(self, account_id, before_trade_date):
        """将所有 trade_date < before_trade_date 的 PENDING 订单设为 EXPIRED。"""
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE paper_orders
                SET status = 'EXPIRED'
                WHERE account_id = ? AND status = 'PENDING' AND trade_date < ?
                """,
                (account_id, before_trade_date),
            )

    def apply_manual_fill(
        self,
        fill_row,
        trade_row,
        position_rows,
        nav_row,
    ):
        """原子执行一笔手工确认成交：更新订单、写入成交、替换持仓、写入 NAV。"""
        with self.connect() as conn:
            now = datetime.now().isoformat(timespec="seconds")
            # 更新订单状态
            conn.execute(
                "UPDATE paper_orders SET status = ?, reason = ? WHERE order_id = ?",
                (fill_row["status"], fill_row.get("reason", ""), fill_row["order_id"]),
            )
            # 写入成交
            conn.execute(
                """
                INSERT INTO paper_trades (
                    trade_id, dedupe_key, order_id, account_id, trade_date, ticker,
                    action, shares, price, commission, source, created_at
                ) VALUES (
                    :trade_id, :dedupe_key, :order_id, :account_id, :trade_date, :ticker,
                    :action, :shares, :price, :commission, :source, :created_at
                )
                """,
                trade_row,
            )
            # 删除旧持仓并写入新持仓
            account_id = nav_row["account_id"]
            trade_date = nav_row["nav_date"]
            conn.execute(
                "DELETE FROM paper_positions WHERE account_id = ? AND as_of_date = ?",
                (account_id, trade_date),
            )
            for row in position_rows:
                conn.execute(
                    """
                    INSERT INTO paper_positions (
                        account_id, as_of_date, ticker, shares, cost_price, last_price, market_value
                    ) VALUES (
                        :account_id, :as_of_date, :ticker, :shares, :cost_price, :last_price, :market_value
                    )
                    """,
                    row,
                )
            # 写入 NAV
            conn.execute(
                """
                INSERT OR REPLACE INTO paper_daily_nav (
                    account_id, nav_date, cash, positions_value, nav, data_date, created_at
                ) VALUES (
                    :account_id, :nav_date, :cash, :positions_value, :nav, :data_date, :created_at
                )
                """,
                dict(nav_row, created_at=nav_row.get("created_at", now)),
            )

    def list_nav_history(self, account_id, start_date=None, end_date=None):
        query = "SELECT * FROM paper_daily_nav WHERE account_id = ?"
        params = [account_id]
        if start_date is not None:
            query += " AND nav_date >= ?"
            params.append(start_date)
        if end_date is not None:
            query += " AND nav_date <= ?"
            params.append(end_date)
        query += " ORDER BY nav_date"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    # Lifecycle helpers

    _UNSET = object()

    def update_account_lifecycle(
        self,
        account_id,
        status=_UNSET,
        is_hidden=_UNSET,
        is_deleted=_UNSET,
        closed_at=_UNSET,
        deleted_at=_UNSET,
        lifecycle_reason=_UNSET,
    ):
        """原子更新账户生命周期字段。显式传入 ``None`` 会清空对应列。

        返回是否实际更新了行。
        """
        with self.connect() as conn:
            now = datetime.now().isoformat(timespec="seconds")
            fields = ["updated_at = ?"]
            params = [now]
            if status is not self._UNSET:
                fields.append("status = ?")
                params.append(status)
            if is_hidden is not self._UNSET:
                fields.append("is_hidden = ?")
                params.append(1 if is_hidden else 0)
            if is_deleted is not self._UNSET:
                fields.append("is_deleted = ?")
                params.append(1 if is_deleted else 0)
            if closed_at is not self._UNSET:
                if closed_at is None:
                    fields.append("closed_at = NULL")
                else:
                    fields.append("closed_at = ?")
                    params.append(closed_at)
            if deleted_at is not self._UNSET:
                if deleted_at is None:
                    fields.append("deleted_at = NULL")
                else:
                    fields.append("deleted_at = ?")
                    params.append(deleted_at)
            if lifecycle_reason is not self._UNSET:
                if lifecycle_reason is None:
                    fields.append("lifecycle_reason = NULL")
                else:
                    fields.append("lifecycle_reason = ?")
                    params.append(lifecycle_reason)
            params.append(account_id)
            cur = conn.execute(
                f"UPDATE paper_accounts SET {', '.join(fields)} WHERE account_id = ?",
                params,
            )
            return cur.rowcount > 0

    def close_account(self, account_id, reason=None):
        """结束账户：在同一事务中更新账户状态并取消所有 PENDING 订单。"""
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE paper_accounts
                SET status = ?, closed_at = ?, lifecycle_reason = ?, updated_at = ?
                WHERE account_id = ?
                """,
                ("ENDED", now, reason or "closed", now, account_id),
            )
            conn.execute(
                """
                UPDATE paper_orders
                SET status = 'CANCELLED', reason = 'ACCOUNT_CLOSED'
                WHERE account_id = ? AND status = 'PENDING'
                """,
                (account_id,),
            )

    def soft_delete_account(self, account_id, reason=None):
        """软删除账户：在同一事务中标记 is_deleted=1、status=ENDED 并取消所有 PENDING 订单。"""
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE paper_accounts
                SET status = ?, is_deleted = 1, deleted_at = ?, lifecycle_reason = ?, updated_at = ?
                WHERE account_id = ?
                """,
                ("ENDED", now, reason or "soft deleted", now, account_id),
            )
            conn.execute(
                """
                UPDATE paper_orders
                SET status = 'CANCELLED', reason = 'ACCOUNT_DELETED'
                WHERE account_id = ? AND status = 'PENDING'
                """,
                (account_id,),
            )

    def restore_account(self, account_id, status="READY", reason=None):
        """恢复软删除账户到 READY 状态。

        恢复时必须同时清空 closed_at 和 deleted_at，确保账户与全新 READY 账户字段一致。
        """
        return self.update_account_lifecycle(
            account_id,
            status=status,
            is_deleted=False,
            closed_at=None,
            deleted_at=None,
            lifecycle_reason=reason or "restored",
        )

    def permanently_delete_account(self, account_id):
        """永久删除账户并级联清理所有子表数据。返回是否删除了账户行。

        删除顺序受外键约束：先 trades（依赖 orders），再 orders，
        最后其他子表和 paper_accounts。
        """
        with self.connect() as conn:
            # 先删依赖 orders 的 trades
            conn.execute(
                "DELETE FROM paper_trades WHERE account_id = ?",
                (account_id,),
            )
            # 再删 orders
            conn.execute(
                "DELETE FROM paper_orders WHERE account_id = ?",
                (account_id,),
            )
            # 其他子表无相互外键依赖，顺序任意
            for table in (
                "paper_positions",
                "paper_daily_nav",
                "paper_runs",
                "paper_signals",
                "paper_skipped",
            ):
                conn.execute(
                    f"DELETE FROM {table} WHERE account_id = ?",
                    (account_id,),
                )
            cur = conn.execute(
                "DELETE FROM paper_accounts WHERE account_id = ?",
                (account_id,),
            )
            return cur.rowcount > 0
