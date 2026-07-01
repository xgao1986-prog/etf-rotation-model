# src/paper_trading/runner.py — daily valuation, stop-loss, weekly rebalance, and simulated execution
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd

from .models import OpeningPosition
from .store import DuplicateLedgerEvent


class PaperTradingRunner:
    """Phase 2: daily operations for a single B0.4 virtual account."""

    COMMISSION_RATE = 0.0003
    MIN_COMMISSION = 5.0
    LOT_SIZE = 100
    NAV_TOLERANCE = 0.01

    def __init__(self, service):
        self.service = service

    # ============== Daily Valuation ==============

    def run_daily_valuation(self, account_id, date, prices):
        """更新持仓市值，记录每日 NAV。"""
        # 获取上一日的持仓和现金
        prev_date = self._previous_date(account_id, date)
        prev_positions = self.service.store.list_positions(account_id, prev_date)
        prev_nav = self.service.store.get_nav(account_id, prev_date)

        if prev_nav is None:
            raise KeyError(f"missing previous NAV: {account_id} {prev_date}")

        cash = prev_nav["cash"]
        positions_value = 0.0
        position_rows = []

        for pos in prev_positions:
            ticker = pos["ticker"]
            shares = pos["shares"]
            price = prices.get(ticker, pos["last_price"])
            market_value = shares * price
            positions_value += market_value
            position_rows.append({
                "account_id": account_id,
                "as_of_date": date,
                "ticker": ticker,
                "shares": shares,
                "cost_price": pos["cost_price"],
                "last_price": price,
                "market_value": market_value,
            })

        nav = cash + positions_value
        now = datetime.now().isoformat(timespec="seconds")
        nav_row = {
            "account_id": account_id,
            "nav_date": date,
            "cash": cash,
            "positions_value": positions_value,
            "nav": nav,
            "data_date": date,
            "created_at": now,
        }
        self.service.store.insert_nav(nav_row)
        if position_rows:
            self.service.store._insert_positions(position_rows)

        return {"cash": cash, "positions_value": positions_value, "nav": nav}

    def _previous_date(self, account_id, date):
        """获取账户的最近一个记录日期。"""
        with self.service.store.connect() as conn:
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
        # 如果没有前一日，返回账户启动日期
        account = self.service.get_account(account_id)
        return account["start_date"]

    # ============== Stop Loss ==============

    def check_stop_loss(self, account_id, date, prices, stop_loss=-0.08):
        """检查持仓是否触发止损。返回 STOP_LOSS 订单列表。"""
        prev_date = self._previous_date(account_id, date)
        positions = self.service.store.list_positions(account_id, prev_date)
        orders = []

        for pos in positions:
            ticker = pos["ticker"]
            cost_price = pos["cost_price"]
            current_price = prices.get(ticker, pos["last_price"])
            if cost_price <= 0:
                continue

            loss_pct = (current_price - cost_price) / cost_price
            # 使用容差避免浮点数精度问题
            if loss_pct < stop_loss - 1e-9:
                order = {
                    "order_id": f"sl-{account_id}-{date}-{ticker}",
                    "dedupe_key": f"{account_id}:{date}:{ticker}:STOP_LOSS",
                    "account_id": account_id,
                    "signal_date": date,
                    "trade_date": date,
                    "ticker": ticker,
                    "action": "STOP_LOSS",
                    "current_shares": pos["shares"],
                    "target_shares": 0,
                    "delta_shares": -pos["shares"],
                    "reference_price": current_price,
                    "reason": f"stop loss triggered at {loss_pct:.2%}",
                    "status": "PENDING",
                }
                orders.append(order)

        return orders

    # ============== Weekly Rebalance ==============

    def run_weekly_rebalance(self, account_id, date, scores_df, prices, cfg):
        """生成 B0.4 调仓订单。"""
        min_score = cfg.get("min_total_score", 40)
        max_holdings = cfg.get("max_holdings", 5)
        max_position = cfg.get("max_position_per_etf", 0.20)

        # 筛选合格候选
        qualified = scores_df[scores_df["total_score"] >= min_score].sort_values(
            "total_score", ascending=False
        )
        candidates = qualified.head(max_holdings)

        # 获取当前持仓
        prev_date = self._previous_date(account_id, date)
        positions = self.service.store.list_positions(account_id, prev_date)
        current_tickers = {p["ticker"] for p in positions}
        target_tickers = set(candidates["ticker"].tolist())

        # 获取当前 NAV
        nav = self.service.store.get_nav(account_id, prev_date)
        if nav is None:
            raise KeyError(f"missing NAV: {account_id} {prev_date}")
        total_nav = nav["nav"]
        max_per_etf_value = total_nav * max_position

        orders = []
        # SELL 旧持仓
        for pos in positions:
            if pos["ticker"] not in target_tickers:
                order = self._make_order(
                    account_id, date, pos["ticker"], "SELL",
                    pos["shares"], 0, prices.get(pos["ticker"], pos["last_price"]),
                    "rebalance: dropped from candidates"
                )
                orders.append(order)

        # BUY 新候选
        for _, row in candidates.iterrows():
            ticker = row["ticker"]
            price = prices.get(ticker, 1.0)
            if price <= 0:
                continue
            # 计算目标股数
            target_shares = int(max_per_etf_value / price / self.LOT_SIZE) * self.LOT_SIZE
            if target_shares <= 0:
                continue

            current_shares = sum(p["shares"] for p in positions if p["ticker"] == ticker)
            delta = target_shares - current_shares

            if delta > 0:
                order = self._make_order(
                    account_id, date, ticker, "BUY",
                    current_shares, target_shares, price,
                    "B0.4 selected"
                )
                orders.append(order)
            elif delta < 0:
                order = self._make_order(
                    account_id, date, ticker, "SELL",
                    current_shares, target_shares, price,
                    "B0.4 reduce position"
                )
                orders.append(order)

        return orders

    def _make_order(self, account_id, date, ticker, action, current_shares, target_shares, price, reason):
        return {
            "order_id": f"{action.lower()}-{account_id}-{date}-{ticker}",
            "dedupe_key": f"{account_id}:{date}:{ticker}:{action}",
            "account_id": account_id,
            "signal_date": date,
            "trade_date": date,
            "ticker": ticker,
            "action": action,
            "current_shares": current_shares,
            "target_shares": target_shares,
            "delta_shares": target_shares - current_shares,
            "reference_price": price,
            "reason": reason,
            "status": "PENDING",
        }

    # ============== Simulated Execution ==============

    def simulate_execution(self, account_id, trade_date, orders, prices, commission_rate=None):
        """模拟 T+1 开盘价成交。"""
        if commission_rate is None:
            commission_rate = self.COMMISSION_RATE

        executed = []
        now = datetime.now().isoformat(timespec="seconds")

        for order in orders:
            ticker = order["ticker"]
            price = prices.get(ticker, order.get("reference_price", 0))
            if price <= 0:
                continue

            delta = order["delta_shares"]
            action = order["action"]
            shares = abs(delta)
            if shares <= 0:
                continue

            # 计算佣金
            commission = max(shares * price * commission_rate, self.MIN_COMMISSION)

            trade = {
                "trade_id": f"trade-{order['order_id']}",
                "dedupe_key": f"{account_id}:{trade_date}:{ticker}:{action}",
                "order_id": order["order_id"],
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
                self.service.store.append_trade(trade)
                executed.append(trade)
            except DuplicateLedgerEvent:
                # 同日重复成交跳过
                continue

        # 更新持仓和现金
        if executed:
            self._update_positions_after_execution(account_id, trade_date, executed, prices)

        return executed

    def _update_positions_after_execution(self, account_id, trade_date, trades, prices):
        """根据成交记录更新持仓和现金。"""
        # 获取上一日状态
        prev_date = self._previous_date(account_id, trade_date)
        prev_positions = {p["ticker"]: p for p in self.service.store.list_positions(account_id, prev_date)}
        prev_nav = self.service.store.get_nav(account_id, prev_date)
        cash = prev_nav["cash"] if prev_nav else 0.0

        position_rows = []
        for ticker, pos in prev_positions.items():
            price = prices.get(ticker, pos["last_price"])
            position_rows.append({
                "account_id": account_id,
                "as_of_date": trade_date,
                "ticker": ticker,
                "shares": pos["shares"],
                "cost_price": pos["cost_price"],
                "last_price": price,
                "market_value": pos["shares"] * price,
            })

        # 应用成交
        for trade in trades:
            ticker = trade["ticker"]
            action = trade["action"]
            shares = trade["shares"]
            price = trade["price"]
            commission = trade["commission"]

            if action == "BUY":
                cash -= shares * price + commission
                # 更新或新建持仓
                existing = next((p for p in position_rows if p["ticker"] == ticker), None)
                if existing:
                    total_cost = existing["cost_price"] * existing["shares"] + price * shares
                    existing["shares"] += shares
                    existing["cost_price"] = total_cost / existing["shares"] if existing["shares"] > 0 else 0
                    existing["last_price"] = price
                    existing["market_value"] = existing["shares"] * price
                else:
                    position_rows.append({
                        "account_id": account_id,
                        "as_of_date": trade_date,
                        "ticker": ticker,
                        "shares": shares,
                        "cost_price": price,
                        "last_price": price,
                        "market_value": shares * price,
                    })
            elif action in ("SELL", "STOP_LOSS"):
                cash += shares * price - commission
                # 移除或减仓持仓
                existing = next((p for p in position_rows if p["ticker"] == ticker), None)
                if existing:
                    existing["shares"] -= shares
                    if existing["shares"] <= 0:
                        position_rows = [p for p in position_rows if p["ticker"] != ticker]
                    else:
                        existing["market_value"] = existing["shares"] * existing["last_price"]

        positions_value = sum(p["market_value"] for p in position_rows)
        nav = cash + positions_value
        now = datetime.now().isoformat(timespec="seconds")
        nav_row = {
            "account_id": account_id,
            "nav_date": trade_date,
            "cash": cash,
            "positions_value": positions_value,
            "nav": nav,
            "data_date": trade_date,
            "created_at": now,
        }
        self.service.store.insert_nav(nav_row)
        if position_rows:
            self.service.store._insert_positions(position_rows)
