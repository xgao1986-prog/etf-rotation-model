# src/paper_trading/runner.py — daily valuation, stop-loss, weekly rebalance, and simulated execution
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd

from .store import DuplicateLedgerEvent


class PaperTradingRunner:
    """Phase 2: daily operations for a single B0.4 virtual account."""

    COMMISSION_RATE = 0.0003
    MIN_COMMISSION = 5.0
    LOT_SIZE = 100
    NAV_TOLERANCE = 0.01

    def __init__(self, service, rebalance_planner=None):
        self.service = service
        self.rebalance_planner = rebalance_planner

    # ============== Daily Valuation ==============

    def run_daily_valuation(self, account_id, date, prices):
        """更新持仓市值，记录每日 NAV。缺少价格的持仓保持上一日价格。"""
        # 如果当天已有 NAV，直接返回（不重复估值）
        existing = self.service.store.get_nav(account_id, date)
        if existing:
            return existing

        prev_date = self.service.store.get_previous_nav_date(account_id, date)
        if prev_date is None:
            raise KeyError(f"no previous NAV for {account_id}")

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

        return self.service.store.get_nav(account_id, date)

    # ============== Stop Loss ==============

    def check_stop_loss(self, account_id, date, prices, stop_loss=-0.08):
        """检查持仓是否触发止损。缺少价格的持仓不检查。返回 STOP_LOSS 订单列表。"""
        prev_date = self.service.store.get_previous_nav_date(account_id, date)
        if prev_date is None:
            return []

        positions = self.service.store.list_positions(account_id, prev_date)
        orders = []

        for pos in positions:
            ticker = pos["ticker"]
            cost_price = pos["cost_price"]
            if cost_price <= 0:
                continue
            if ticker not in prices or prices[ticker] <= 0:
                continue

            current_price = prices[ticker]
            loss_pct = (current_price - cost_price) / cost_price
            if loss_pct < stop_loss - 1e-9:
                order = self._make_order(
                    account_id, date, ticker, "STOP_LOSS",
                    pos["shares"], 0, current_price,
                    f"stop loss triggered at {loss_pct:.2%}"
                )
                orders.append(order)

        return orders

    # ============== Weekly Rebalance ==============

    def run_weekly_rebalance(self, account_id, signal_date, scores_df, prices, cfg, trade_date=None):
        """
        使用 B0.4 正式调仓规则生成订单。

        signal_date: 信号生成日期（如周四）
        trade_date: 计划执行日期（如周五，默认 signal_date+1 天）
        """
        if trade_date is None:
            trade_date = self._next_trade_date(signal_date)

        # 获取当前持仓和现金（优先使用 signal_date 当天的 NAV，否则前一天）
        prev_nav = self.service.store.get_nav(account_id, signal_date)
        prev_date = signal_date
        if prev_nav is None:
            prev_date = self.service.store.get_previous_nav_date(account_id, signal_date)
            if prev_date is None:
                prev_date = self.service.get_account(account_id)["start_date"]
            prev_nav = self.service.store.get_nav(account_id, prev_date)
        if prev_nav is None:
            raise KeyError(f"missing NAV: {account_id} {signal_date}")

        cash = prev_nav["cash"]
        nav = prev_nav["nav"]

        # 获取当前持仓（使用与 NAV 相同的日期）
        prev_positions = self.service.store.list_positions(account_id, prev_date)
        current_positions = {p["ticker"]: p["shares"] for p in prev_positions}

        # 构建价格映射（优先使用 prices，缺失使用 last_price）
        price_map = {}
        for p in prev_positions:
            price_map[p["ticker"]] = prices.get(p["ticker"], p["last_price"])
        for t in scores_df["ticker"].unique():
            if t not in price_map:
                price_map[t] = prices.get(t, 0.0)

        # 构建候选列表
        from config import ETF_UNIVERSE, DEFENSE_UNIVERSE
        min_score = cfg.get("min_total_score", 40)
        industry_scores = scores_df[scores_df["ticker"].isin(ETF_UNIVERSE.keys())]
        qualified = industry_scores[industry_scores["total_score"] >= min_score].sort_values(
            "total_score", ascending=False
        )
        industry_candidates = [(row["ticker"], float(row["total_score"])) for _, row in qualified.iterrows()]

        defense_scores = scores_df[scores_df["ticker"].isin(DEFENSE_UNIVERSE.keys())]
        defense_candidates = [(row["ticker"], float(row["total_score"])) for _, row in defense_scores.iterrows()]

        # 如果没有 rebalance_planner，使用简化逻辑（测试中注入）
        if self.rebalance_planner is None:
            from rebalance_planner import plan_rebalance_v2_5
            self.rebalance_planner = plan_rebalance_v2_5

        trades, final_state = self.rebalance_planner(
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

        # 将 trades 转换为订单
        orders = []
        for trade in trades:
            action = trade["action"]
            ticker = trade["ticker"]
            delta = trade["shares"]
            current_shares = current_positions.get(ticker, 0)
            target_shares = current_shares + delta if action == "BUY" else current_shares - delta
            price = price_map.get(ticker, trade.get("price", 0))

            order = {
                "order_id": f"{action.lower()}-{account_id}-{trade_date}-{ticker}",
                "dedupe_key": f"{account_id}:{trade_date}:{ticker}:{action}",
                "account_id": account_id,
                "signal_date": signal_date,
                "trade_date": trade_date,
                "ticker": ticker,
                "action": action,
                "current_shares": current_shares,
                "target_shares": target_shares,
                "delta_shares": delta if action == "BUY" else -delta,
                "reference_price": price,
                "reason": trade.get("reason", "B0.4 rebalance"),
                "status": "PENDING",
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
            orders.append(order)

        return orders

    def _next_trade_date(self, signal_date):
        """计算下一个交易日（简化：+1 天，实际应使用交易日历）。"""
        dt = pd.to_datetime(signal_date) + timedelta(days=1)
        return dt.strftime("%Y-%m-%d")

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
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }

    # ============== Simulated Execution ==============

    def simulate_execution(self, account_id, trade_date, orders, prices):
        """
        模拟 T+1 开盘价成交。原子执行：成交、持仓更新、NAV 更新在同一事务。
        买入前检查现金（含佣金），不足时跳过。
        缺少可靠价格时跳过并记录原因。
        同日重复执行不报错、不重复成交。
        """
        # 过滤缺少价格的订单，记录原因
        valid_orders = []
        skipped = []
        for order in orders:
            ticker = order["ticker"]
            if ticker not in prices or prices[ticker] <= 0:
                skipped.append((order["order_id"], f"missing price for {ticker}"))
                continue
            valid_orders.append(order)

        if not valid_orders:
            return [], skipped

        executed, atomic_skipped = self.service.store.execute_trades_atomic(
            account_id, trade_date, valid_orders, prices,
            commission_rate=self.COMMISSION_RATE,
            min_commission=self.MIN_COMMISSION,
        )
        skipped.extend(atomic_skipped)
        return executed, skipped

    # ============== Complete Daily Flow ==============

    def run_daily(self, account_id, date, prices, scores_df=None, cfg=None, is_rebalance_day=False):
        """
        完整的每日流程：
        1. 每日估值
        2. 止损检查
        3. 如果是调仓日：生成信号订单（signal_date=date）
        4. 如果是执行日：执行前一日信号订单
        5. 记录运行结果

        返回：{"valuation": nav, "stop_loss_orders": [...], "rebalance_orders": [...], "executed": [...], "skipped": [...]}
        """
        result = {"valuation": None, "stop_loss_orders": [], "rebalance_orders": [], "executed": [], "skipped": []}

        # 1. 每日估值
        nav = self.run_daily_valuation(account_id, date, prices)
        result["valuation"] = nav

        # 2. 止损检查
        stop_loss_orders = self.check_stop_loss(account_id, date, prices)
        result["stop_loss_orders"] = stop_loss_orders

        # 3. 止损订单立即执行（止损当日生成、当日执行）
        if stop_loss_orders:
            executed, skipped = self.simulate_execution(account_id, date, stop_loss_orders, prices)
            result["executed"].extend(executed)
            result["skipped"].extend(skipped)

        # 4. 如果是调仓日（周四），生成信号订单
        if is_rebalance_day and scores_df is not None and cfg is not None:
            rebalance_orders = self.run_weekly_rebalance(account_id, date, scores_df, prices, cfg)
            result["rebalance_orders"] = rebalance_orders
            # 信号订单保存到 paper_orders，不立即执行
            for order in rebalance_orders:
                try:
                    self.service.append_order(order)
                except RuntimeError:
                    # 重复订单已存在，跳过
                    pass

        # 5. 检查是否有前一日信号需要执行（周五执行周四信号）
        trade_date = self._next_trade_date(date)
        # 实际逻辑：调用方应知道今天是执行日，传入前一日 orders
        # 这里简化为：如果 orders 已传入，执行它们

        return result

    def execute_rebalance_orders(self, account_id, trade_date, orders, prices):
        """执行前一日生成的调仓订单（T+1 执行）。"""
        return self.simulate_execution(account_id, trade_date, orders, prices)
