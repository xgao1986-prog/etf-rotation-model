# src/paper_trading/runner.py — daily valuation, stop-loss, weekly rebalance, and simulated execution
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd

from .store import DuplicateLedgerEvent


class TradingCalendar:
    """简单交易日历：跳过周末。"""

    @staticmethod
    def is_trading_day(date: str) -> bool:
        dt = pd.to_datetime(date)
        return dt.weekday() < 5  # Monday=0, Friday=4

    @staticmethod
    def next_trading_day(date: str) -> str:
        dt = pd.to_datetime(date) + timedelta(days=1)
        while dt.weekday() >= 5:
            dt += timedelta(days=1)
        return dt.strftime("%Y-%m-%d")

    @staticmethod
    def previous_trading_day(date: str) -> str:
        dt = pd.to_datetime(date) - timedelta(days=1)
        while dt.weekday() >= 5:
            dt -= timedelta(days=1)
        return dt.strftime("%Y-%m-%d")

    @staticmethod
    def is_rebalance_day(date: str) -> bool:
        """周四为调仓日。"""
        return pd.to_datetime(date).weekday() == 3


class PaperTradingRunner:
    """Phase 2: daily operations for a single B0.4 virtual account."""

    COMMISSION_RATE = 0.0003
    MIN_COMMISSION = 5.0
    LOT_SIZE = 100
    NAV_TOLERANCE = 0.01
    DEFENSE_THRESHOLD_DELTA = 10

    def __init__(self, service, rebalance_planner=None):
        self.service = service
        self.rebalance_planner = rebalance_planner
        self.calendar = TradingCalendar()

    # ============== Daily Flow (single entry point) ==============

    def run_daily(self, account_id, date, open_prices, close_prices, scores_df=None, cfg=None):
        """
        完整每日流程（幂等）。

        1. 如果当天已处理，直接返回已有结果。
        2. 获取上一交易日状态作为基线。
        3. 执行今天到期的调仓信号（使用 open_prices）。
        4. 每日估值（使用 close_prices）。
        5. 止损检查（使用 close_prices）。
        6. 执行止损（使用 close_prices）。
        7. 如果是调仓日，保存信号供下一交易日执行。
        8. 原子保存最终状态。

        返回：{"cash": float, "positions": dict, "nav": float, "trades": list, "skipped": list}
        """
        # 1. 幂等检查
        if self.service.store.is_day_processed(account_id, date):
            return self._build_result_from_db(account_id, date)

        # 2. 获取基线状态
        prev_date = self.calendar.previous_trading_day(date)
        baseline = self._get_baseline_state(account_id, prev_date)

        trades = []
        skipped = []

        # 3. 执行今天到期的调仓信号
        pending_signals = self.service.store.load_pending_signals(account_id, date)
        for signal in pending_signals:
            signal_trades, signal_skipped = self._execute_signal(
                account_id, date, baseline, signal, open_prices
            )
            trades.extend(signal_trades)
            skipped.extend(signal_skipped)

        # 4. 每日估值（更新持仓市值，不修改股数）
        baseline = self._apply_valuation(baseline, close_prices)

        # 5. 止损检查
        sl_orders = self._check_stop_loss(baseline, close_prices)

        # 6. 执行止损
        if sl_orders:
            sl_trades, sl_skipped = self._execute_orders(
                account_id, date, sl_orders, close_prices
            )
            trades.extend(sl_trades)
            skipped.extend(sl_skipped)
            # 更新基线状态（止损已执行）
            baseline = self._apply_trades_to_state(baseline, sl_trades, close_prices)

        # 7. 如果是调仓日，保存信号
        if self.calendar.is_rebalance_day(date) and scores_df is not None and cfg is not None:
            next_day = self.calendar.next_trading_day(date)
            self._save_rebalance_signal(account_id, date, next_day, scores_df, cfg)

        # 8. 原子保存最终状态
        self.service.store.save_daily_state(
            account_id, date,
            cash=baseline["cash"],
            positions=baseline["positions"],
            trades=trades,
        )

        return self._build_result_from_db(account_id, date)

    # ============== State Management ==============

    def _get_baseline_state(self, account_id, prev_date):
        """获取上一交易日状态作为今日基线。如果 prev_date 无记录，回退到最近可用记录。"""
        nav = self.service.store.get_nav(account_id, prev_date)
        if nav is None:
            # 回退到最近可用 NAV
            nav_date = self.service.store.get_previous_nav_date(account_id, prev_date)
            if nav_date is None:
                raise KeyError(f"missing NAV: {account_id} {prev_date}")
            nav = self.service.store.get_nav(account_id, nav_date)
            prev_date = nav_date

        positions = self.service.store.list_positions(account_id, prev_date)
        return {
            "cash": nav["cash"],
            "positions": {
                p["ticker"]: {
                    "shares": p["shares"],
                    "cost_price": p["cost_price"],
                    "last_price": p["last_price"],
                }
                for p in positions
            },
        }

    def _apply_valuation(self, state, prices):
        """更新持仓市值（不修改股数）。"""
        for ticker, pos in state["positions"].items():
            price = prices.get(ticker, pos["last_price"])
            pos["last_price"] = price
        return state

    def _check_stop_loss(self, state, prices, stop_loss=-0.08):
        """检查持仓是否触发止损。缺少价格的不检查。"""
        orders = []
        for ticker, pos in state["positions"].items():
            cost_price = pos["cost_price"]
            if cost_price <= 0:
                continue
            if ticker not in prices or prices[ticker] <= 0:
                continue
            current_price = prices[ticker]
            loss_pct = (current_price - cost_price) / cost_price
            if loss_pct < stop_loss - 1e-9:
                orders.append({
                    "order_id": f"sl-{ticker}-{datetime.now().strftime('%Y%m%d')}",
                    "ticker": ticker,
                    "action": "STOP_LOSS",
                    "delta_shares": -pos["shares"],
                })
        return orders

    def _execute_orders(self, account_id, trade_date, orders, prices):
        """执行订单，返回 (trades, skipped)。"""
        return self.service.store.execute_trades_atomic(
            account_id, trade_date, orders, prices,
            commission_rate=self.COMMISSION_RATE,
            min_commission=self.MIN_COMMISSION,
        )

    def _apply_trades_to_state(self, state, trades, prices):
        """将成交记录应用到状态（内存更新，不写入数据库）。"""
        for trade in trades:
            ticker = trade["ticker"]
            action = trade["action"]
            shares = trade["shares"]
            price = trade["price"]
            commission = trade["commission"]

            if action == "BUY":
                state["cash"] -= shares * price + commission
                if ticker in state["positions"]:
                    p = state["positions"][ticker]
                    total_cost = p["cost_price"] * p["shares"] + price * shares
                    p["shares"] += shares
                    p["cost_price"] = total_cost / p["shares"] if p["shares"] > 0 else 0
                    p["last_price"] = price
                else:
                    state["positions"][ticker] = {
                        "shares": shares, "cost_price": price, "last_price": price,
                    }
            elif action in ("SELL", "STOP_LOSS"):
                state["cash"] += shares * price - commission
                if ticker in state["positions"]:
                    p = state["positions"][ticker]
                    p["shares"] -= shares
                    if p["shares"] <= 0:
                        del state["positions"][ticker]
        return state

    # ============== Signal Execution ==============

    def _execute_signal(self, account_id, trade_date, baseline, signal, open_prices):
        """执行保存的信号：重新运行 planner，使用当日开盘价。"""
        from io import StringIO
        scores_df = pd.read_json(StringIO(signal["scores_json"]))
        cfg = json.loads(signal["config_json"])

        trades, _ = self._run_planner(baseline, scores_df, open_prices, cfg)

        # 将 planner trades 转换为 orders
        orders = []
        for trade in trades:
            action = trade["action"]
            ticker = trade["ticker"]
            shares = trade["shares"]
            current_shares = baseline["positions"].get(ticker, {}).get("shares", 0)
            delta = shares if action == "BUY" else -shares
            orders.append({
                "order_id": f"{action.lower()}-{account_id}-{trade_date}-{ticker}",
                "ticker": ticker,
                "action": action,
                "delta_shares": delta,
            })

        return self._execute_orders(account_id, trade_date, orders, open_prices)

    # ============== Rebalance Planner ==============

    def _run_planner(self, state, scores_df, prices, cfg):
        """使用 plan_rebalance_v2_5 生成调仓计划。"""
        from config import ETF_UNIVERSE, DEFENSE_UNIVERSE

        cash = state["cash"]
        nav = cash + sum(
            p["shares"] * p["last_price"] for p in state["positions"].values()
        )
        current_positions = {t: p["shares"] for t, p in state["positions"].items()}

        min_score = cfg.get("min_total_score", 40)

        # 行业候选
        industry_scores = scores_df[scores_df["ticker"].isin(ETF_UNIVERSE.keys())]
        qualified = industry_scores[industry_scores["total_score"] >= min_score].sort_values(
            "total_score", ascending=False
        )
        industry_candidates = [(row["ticker"], float(row["total_score"])) for _, row in qualified.iterrows()]

        # 防御候选：门槛 = 行业门槛 - 10
        defense_min_score = min_score - self.DEFENSE_THRESHOLD_DELTA
        defense_scores = scores_df[scores_df["ticker"].isin(DEFENSE_UNIVERSE.keys())]
        defense_qualified = defense_scores[defense_scores["total_score"] >= defense_min_score].sort_values(
            "total_score", ascending=False
        )
        defense_candidates = [(row["ticker"], float(row["total_score"])) for _, row in defense_qualified.iterrows()]

        # 构建价格映射
        price_map = {}
        for t in set(ETF_UNIVERSE.keys()) | set(DEFENSE_UNIVERSE.keys()):
            price_map[t] = prices.get(t, 0.0)
        for t, p in state["positions"].items():
            if t not in price_map or price_map[t] <= 0:
                price_map[t] = p["last_price"]

        planner = self.rebalance_planner or self._default_planner
        return planner(
            nav=nav,
            cash=cash,
            current_positions=current_positions,
            industry_candidates=industry_candidates,
            defense_candidates=defense_candidates,
            prices=price_map,
            industry_tickers=set(ETF_UNIVERSE.keys()),
            defense_tickers=set(DEFENSE_UNIVERSE.keys()),
            max_industry_holdings=cfg.get("max_holdings", 5),
            max_defense_holdings=2,
            max_total_holdings=cfg.get("max_holdings", 5),
            max_position_per_etf=cfg.get("max_position_per_etf", 0.20),
            max_total_position=1.0,
            commission_rate=self.COMMISSION_RATE,
            min_commission=self.MIN_COMMISSION,
            lot_size=self.LOT_SIZE,
            defense_enabled=True,
        )

    def _default_planner(self, **kwargs):
        from rebalance_planner import plan_rebalance_v2_5
        return plan_rebalance_v2_5(**kwargs)

    # ============== Signal Persistence ==============

    def _save_rebalance_signal(self, account_id, signal_date, trade_date, scores_df, cfg):
        """保存评分和配置，供下一交易日执行。"""
        scores_json = scores_df.to_json(orient="records")
        config_json = json.dumps(cfg, sort_keys=True)
        self.service.store.save_signals(account_id, signal_date, trade_date, scores_json, config_json)

    # ============== Result Building ==============

    def _build_result_from_db(self, account_id, date):
        """从数据库构建返回结果。"""
        nav = self.service.store.get_nav(account_id, date)
        positions = self.service.store.list_positions(account_id, date)
        trades = self.service.store.list_trades(account_id, date)
        return {
            "cash": nav["cash"] if nav else 0.0,
            "positions": {p["ticker"]: p for p in positions},
            "nav": nav["nav"] if nav else 0.0,
            "trades": trades,
            "skipped": [],
        }

    # ============== Backward-compatible helpers ==============

    def run_daily_valuation(self, account_id, date, prices):
        """向后兼容：直接调用 run_daily 的估值部分。"""
        return self.run_daily(account_id, date, prices, prices)

    def check_stop_loss(self, account_id, date, prices, stop_loss=-0.08):
        """向后兼容：返回止损订单列表。"""
        prev_date = self.service.store.get_previous_nav_date(account_id, date)
        if prev_date is None:
            return []
        state = self._get_baseline_state(account_id, prev_date)
        return self._check_stop_loss(state, prices, stop_loss)

    def simulate_execution(self, account_id, trade_date, orders, prices):
        """向后兼容：执行订单。"""
        return self._execute_orders(account_id, trade_date, orders, prices)
