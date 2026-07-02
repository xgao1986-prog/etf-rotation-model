# src/paper_trading/runner.py — daily valuation, stop-loss, weekly rebalance, and simulated execution
from __future__ import annotations

import json
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd

from .calendar import ChinaTradingCalendar
from .models import AccountType, OrderStatus


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
        self.calendar = ChinaTradingCalendar()

    # ============== Daily Flow (single entry point) ==============

    def run_daily(self, account_id, date, open_prices, close_prices, scores_df=None):
        """
        完整每日流程（幂等）。

        核心原则：一天的所有计算先在内存完成，最后一次性保存订单、成交、现金、
        持仓、总资产、运行结果和信号。中途不得单独写入账户状态。

        1. 检查 date 是否为交易日且在日历范围内。
        2. 如果当天已处理，直接返回已有结果。
        3. 获取上一交易日状态作为基线。
        4. 执行今天到期的调仓信号（使用 open_prices，先卖后买）。
        5. 止损检查与执行（使用 open_prices）。
        6. 每日估值（使用 close_prices，只更新市值，不修改股数）。
        7. 如果是调仓日，准备信号供下一交易日执行（使用账户冻结配置）。
        8. 原子保存最终状态。

        返回：{"cash": float, "positions": dict, "nav": float, "trades": list, "skipped": list}
        """
        # 1. 交易日与日历范围检查
        if not self.calendar._days:
            raise RuntimeError("trading calendar is empty")
        if date < self.calendar.min_date or date > self.calendar.max_date:
            raise ValueError(
                f"{date} is outside trading calendar range "
                f"[{self.calendar.min_date} ~ {self.calendar.max_date}]; "
                f"please update the calendar cache"
            )
        if not self.calendar.is_trading_day(date):
            raise ValueError(f"{date} is not a trading day")

        # 2. 幂等检查
        if self.service.store.is_day_processed(account_id, date):
            return self._build_result_from_db(account_id, date)

        # 2.5 获取账户类型
        account = self.service.store.get_account(account_id)
        if account is None:
            raise KeyError(f"missing account: {account_id}")
        account_type = AccountType(account["account_type"])
        is_shadow = account_type is AccountType.SHADOW

        # 3. 影子账户：先过期上一交易日的 PENDING 订单
        if is_shadow:
            self.service.store.expire_pending_orders(account_id, before_trade_date=date)

        # 4. 获取基线状态
        prev_date = self.calendar.previous_trading_day(date)
        baseline = self._get_baseline_state(account_id, prev_date)

        trades: List[Dict] = []
        skipped: List[Dict] = []
        orders: List[Dict] = []
        signals: List[Dict] = []

        # 5. 执行今天到期的调仓信号
        pending_signals = self.service.store.load_pending_signals(account_id, date)
        for signal in pending_signals:
            if is_shadow:
                signal_orders = self._build_signal_orders(
                    account_id, date, baseline, signal, open_prices
                )
                orders.extend(signal_orders)
            else:
                signal_trades, signal_skipped, signal_orders = self._execute_signal(
                    account_id, date, baseline, signal, open_prices
                )
                trades.extend(signal_trades)
                skipped.extend(signal_skipped)
                orders.extend(signal_orders)
                baseline = self._apply_trades_to_state(baseline, signal_trades, open_prices)

        # 6. 止损检查与执行（使用开盘价）
        sl_orders, sl_skipped = self._check_stop_loss(account_id, date, baseline, open_prices)
        skipped.extend(sl_skipped)
        if sl_orders:
            if is_shadow:
                for order in sl_orders:
                    order["status"] = OrderStatus.PENDING.value
                orders.extend(sl_orders)
            else:
                sl_trades, sl_skipped, sl_orders_out = self._execute_orders_in_memory(
                    account_id, date, baseline, sl_orders, open_prices
                )
                trades.extend(sl_trades)
                skipped.extend(sl_skipped)
                orders.extend(sl_orders_out)
                baseline = self._apply_trades_to_state(baseline, sl_trades, open_prices)

        # 7. 每日估值（使用收盘价，只更新市值）
        baseline = self._apply_valuation(baseline, close_prices)

        # 8. 如果是调仓日，准备信号（与当天状态一起保存）
        if self.calendar.is_rebalance_day(date) and scores_df is not None:
            cfg = json.loads(account["config_json"])
            next_day = self.calendar.next_trading_day(date)
            signals.append(self._prepare_rebalance_signal(account_id, date, next_day, scores_df, cfg))

        # 9. 原子保存最终状态
        self.service.store.save_daily_state(
            account_id, date,
            cash=baseline["cash"],
            positions=baseline["positions"],
            trades=trades,
            skipped=skipped,
            orders=orders,
            signals=signals,
        )

        return self._build_result_from_db(account_id, date)

    # ============== State Management ==============

    def _get_baseline_state(self, account_id, prev_date):
        """获取上一交易日状态作为今日基线。如果 prev_date 无记录，回退到最近可用记录。"""
        nav = self.service.store.get_nav(account_id, prev_date)
        if nav is None:
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
        """更新持仓市值（不修改股数）。缺少价格时保留最近有效价格。"""
        for ticker, pos in state["positions"].items():
            price = prices.get(ticker, pos["last_price"])
            if price is None or price <= 0:
                continue
            pos["last_price"] = price
        return state

    def _check_stop_loss(self, account_id, trade_date, state, prices, stop_loss=-0.08):
        """检查持仓是否触发止损。缺少开盘价的记录未执行原因。"""
        orders = []
        skipped = []
        now = datetime.now().isoformat(timespec="seconds")
        for ticker, pos in state["positions"].items():
            cost_price = pos["cost_price"]
            if cost_price <= 0:
                continue
            if ticker not in prices or prices[ticker] <= 0:
                skipped.append({
                    "order_id": None,
                    "ticker": ticker,
                    "action": "STOP_LOSS",
                    "reason": f"missing price for {ticker}",
                })
                continue
            current_price = prices[ticker]
            loss_pct = (current_price - cost_price) / cost_price
            if loss_pct < stop_loss - 1e-9:
                order_id = f"sl-{account_id}-{trade_date}-{ticker}"
                orders.append({
                    "order_id": order_id,
                    "dedupe_key": f"{account_id}:{trade_date}:{ticker}:STOP_LOSS",
                    "account_id": account_id,
                    "signal_date": trade_date,
                    "trade_date": trade_date,
                    "ticker": ticker,
                    "action": "STOP_LOSS",
                    "current_shares": pos["shares"],
                    "target_shares": 0,
                    "delta_shares": -pos["shares"],
                    "reference_price": current_price,
                    "reason": "stop-loss triggered at open",
                    "status": "PENDING",
                    "created_at": now,
                })
        return orders, skipped

    def _execute_orders_in_memory(
        self, account_id, trade_date, state, orders, prices
    ) -> Tuple[List[Dict], List[Dict], List[Dict]]:
        """
        在内存中按"先卖后买"顺序执行订单，返回 (trades, skipped, orders)。
        卖出所得立即释放到可用现金，可用于当天后续买入。
        不写入数据库；数据库写入由 run_daily 最后统一完成。
        """
        trades: List[Dict] = []
        skipped: List[Dict] = []
        now = datetime.now().isoformat(timespec="seconds")

        # 先卖后买：确保卖出释放的现金可用于当天买入
        def _sort_key(o):
            action = o.get("action", "")
            return (0 if action in ("SELL", "STOP_LOSS") else 1, o.get("ticker", ""))

        sorted_orders = sorted(orders, key=_sort_key)
        available_cash = state["cash"]

        for order in sorted_orders:
            order.setdefault("status", "PENDING")
            ticker = order["ticker"]
            action = order["action"]
            price = prices.get(ticker)

            if price is None or price <= 0:
                order["status"] = "SKIPPED"
                skipped.append({
                    "order_id": order.get("order_id"),
                    "ticker": ticker,
                    "action": action,
                    "reason": f"missing price for {ticker}",
                })
                continue

            shares = abs(order["delta_shares"])
            if shares <= 0:
                order["status"] = "CANCELLED"
                continue

            commission = max(shares * price * self.COMMISSION_RATE, self.MIN_COMMISSION)

            if action in ("SELL", "STOP_LOSS"):
                available = state["positions"].get(ticker, {}).get("shares", 0)
                if shares > available:
                    order["status"] = "SKIPPED"
                    skipped.append({
                        "order_id": order.get("order_id"),
                        "ticker": ticker,
                        "action": action,
                        "reason": f"oversell: want {shares}, have {available}",
                    })
                    continue
                available_cash += shares * price - commission
            elif action == "BUY":
                cost = shares * price + commission
                if available_cash < cost:
                    order["status"] = "SKIPPED"
                    skipped.append({
                        "order_id": order.get("order_id"),
                        "ticker": ticker,
                        "action": action,
                        "reason": f"insufficient cash: need {cost:.2f}, have {available_cash:.2f}",
                    })
                    continue
                available_cash -= cost
            else:
                order["status"] = "SKIPPED"
                skipped.append({
                    "order_id": order.get("order_id"),
                    "ticker": ticker,
                    "action": action,
                    "reason": f"unknown action: {action}",
                })
                continue

            order_id = order.get("order_id")
            trade_id = f"trade-{account_id}-{trade_date}-{ticker}-{action}"
            trade = {
                "trade_id": trade_id,
                "dedupe_key": f"{account_id}:{trade_date}:{ticker}:{action}",
                "order_id": order_id,
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
            trades.append(trade)
            order["status"] = "FILLED"

        return trades, skipped, sorted_orders

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

    def _build_signal_orders(self, account_id, trade_date, baseline, signal, open_prices):
        """生成调仓信号对应的 PENDING 订单（用于影子账户，不执行）。"""
        from io import StringIO
        scores_df = pd.read_json(StringIO(signal["scores_json"]))
        signal_date = signal["signal_date"]
        now = datetime.now().isoformat(timespec="seconds")

        cfg = json.loads(self.service.store.get_account(account_id)["config_json"])
        planner_trades, _ = self._run_planner(baseline, scores_df, open_prices, cfg)

        orders = []
        for trade in planner_trades:
            action = trade["action"]
            ticker = trade["ticker"]
            shares = trade["shares"]
            delta = shares if action == "BUY" else -shares
            current_shares = baseline["positions"].get(ticker, {}).get("shares", 0)
            target_shares = current_shares + delta
            order_id = f"{action.lower()}-{account_id}-{trade_date}-{ticker}"
            orders.append({
                "order_id": order_id,
                "dedupe_key": f"{account_id}:{trade_date}:{ticker}:{action}",
                "account_id": account_id,
                "signal_date": signal_date,
                "trade_date": trade_date,
                "ticker": ticker,
                "action": action,
                "current_shares": current_shares,
                "target_shares": target_shares,
                "delta_shares": delta,
                "reference_price": trade["price"],
                "reason": trade.get("reason", "rebalance"),
                "status": OrderStatus.PENDING.value,
                "created_at": now,
            })
        return orders

    def _execute_signal(self, account_id, trade_date, baseline, signal, open_prices):
        """执行保存的信号：强制使用账户创建时保存的冻结配置。"""
        orders = self._build_signal_orders(account_id, trade_date, baseline, signal, open_prices)
        return self._execute_orders_in_memory(account_id, trade_date, baseline, orders, open_prices)

    # ============== Rebalance Planner ==============

    def _run_planner(self, state, scores_df, prices, cfg=None):
        """使用 plan_rebalance_v2_5 生成调仓计划。NAV 使用当日开盘价重新计算。"""
        from config import ETF_UNIVERSE, DEFENSE_UNIVERSE

        cash = state["cash"]
        # 使用当日开盘价重新计算持仓总值，不再使用前一日收盘市值
        nav = cash + sum(
            p["shares"] * prices.get(t, p["last_price"])
            for t, p in state["positions"].items()
        )
        current_positions = {t: p["shares"] for t, p in state["positions"].items()}

        # 使用账户冻结配置；如显式传入 cfg（向后兼容）则合并
        account_cfg = {}
        if cfg is not None:
            account_cfg = cfg
        min_score = account_cfg.get("min_total_score", 40)

        industry_scores = scores_df[scores_df["ticker"].isin(ETF_UNIVERSE.keys())]
        qualified = industry_scores[industry_scores["total_score"] >= min_score].sort_values(
            "total_score", ascending=False
        )
        industry_candidates = [(row["ticker"], float(row["total_score"])) for _, row in qualified.iterrows()]

        defense_min_score = min_score - self.DEFENSE_THRESHOLD_DELTA
        defense_scores = scores_df[scores_df["ticker"].isin(DEFENSE_UNIVERSE.keys())]
        defense_qualified = defense_scores[defense_scores["total_score"] >= defense_min_score].sort_values(
            "total_score", ascending=False
        )
        defense_candidates = [(row["ticker"], float(row["total_score"])) for _, row in defense_qualified.iterrows()]

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
            max_industry_holdings=account_cfg.get("max_holdings", 5),
            max_defense_holdings=2,
            max_total_holdings=account_cfg.get("max_holdings", 5),
            max_position_per_etf=account_cfg.get("max_position_per_etf", 0.20),
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

    def _prepare_rebalance_signal(self, account_id, signal_date, trade_date, scores_df, cfg):
        """准备调仓信号，由 run_daily 最终统一保存。"""
        scores_json = scores_df.to_json(orient="records")
        config_json = json.dumps(cfg, sort_keys=True)
        return {
            "account_id": account_id,
            "signal_date": signal_date,
            "trade_date": trade_date,
            "scores_json": scores_json,
            "config_json": config_json,
        }

    # ============== Result Building ==============

    def _build_result_from_db(self, account_id, date):
        """从数据库构建返回结果。"""
        nav = self.service.store.get_nav(account_id, date)
        positions = self.service.store.list_positions(account_id, date)
        trades = self.service.store.list_trades(account_id, date)
        skipped = self.service.store.list_skipped(account_id, date)
        return {
            "cash": nav["cash"] if nav else 0.0,
            "positions": {p["ticker"]: p for p in positions},
            "nav": nav["nav"] if nav else 0.0,
            "trades": trades,
            "skipped": skipped,
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
        orders, _ = self._check_stop_loss(account_id, date, state, prices, stop_loss)
        return orders

    def simulate_execution(self, account_id, trade_date, orders, prices):
        """向后兼容：在内存中执行订单，不写入数据库。"""
        prev_date = self.service.store.get_previous_nav_date(account_id, trade_date)
        if prev_date is None:
            prev_date = self.service.store.get_account(account_id)["start_date"]
        state = self._get_baseline_state(account_id, prev_date)
        trades, skipped, _ = self._execute_orders_in_memory(
            account_id, trade_date, state, orders, prices
        )
        return trades, skipped


def date_str() -> str:
    return datetime.now().strftime("%Y%m%d")
