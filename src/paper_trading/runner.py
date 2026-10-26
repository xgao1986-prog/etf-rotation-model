# src/paper_trading/runner.py — daily valuation, stop-loss, weekly rebalance, and simulated execution
from __future__ import annotations

import json
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd

from .calendar import ChinaTradingCalendar
from .models import AccountType, OrderStatus, resolve_execution_config
from .entry_filter import select_candidates


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

    def run_daily(self, account_id, date, open_prices, close_prices, scores_df=None,
                  corporate_actions=None):
        """
        完整每日流程（幂等）。

        核心原则：一天的所有计算先在内存完成，最后一次性保存订单、成交、现金、
        持仓、总资产、运行结果和信号。中途不得单独写入账户状态。

        1. 检查 date 是否为交易日且在日历范围内。
        2. 获取账户并检查生命周期状态（已删除或已结束账户拒绝运行）。
        3. 如果当天已处理，直接返回已有结果。
        4. 获取上一交易日状态作为基线。
        5. 盘前公司行动调整（corporate_actions，如份额拆分：股数×ratio、成本÷ratio）。
        6. 执行今天到期的调仓信号（使用 open_prices，先卖后买）。
        7. 止损检查与执行（使用 open_prices）。
        8. 每日估值（使用 close_prices，只更新市值，不修改股数）。
        9. 如果是调仓日，准备信号供下一交易日执行（使用账户冻结配置）。
        10. 原子保存最终状态。

        corporate_actions: 可选 list[dict]，每项含 ticker/effective_date/action_type/ratio。
        仅调整 effective_date == date 的行动；市值与总资产不变。幂等由第 3 步保证。

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

        # 2. 获取账户并检查生命周期状态（必须在幂等检查之前）
        account = self.service.store.get_account(account_id)
        if account is None:
            raise KeyError(f"missing account: {account_id}")
        if account.get("is_deleted"):
            raise ValueError(f"account is deleted: {account_id}")
        if account["status"] == "ENDED":
            raise ValueError(f"account is ended: {account_id}")

        # 3. 幂等检查
        if self.service.store.is_day_processed(account_id, date):
            return self._build_result_from_db(account_id, date)

        account_type = AccountType(account["account_type"])
        is_shadow = account_type is AccountType.SHADOW

        # 4. 影子账户：先过期上一交易日的 PENDING 订单
        if is_shadow:
            self.service.store.expire_pending_orders(account_id, before_trade_date=date)

        # 5. 获取基线状态
        prev_date = self.calendar.previous_trading_day(date)
        baseline = self._get_baseline_state(account_id, prev_date)

        # 5.4 跳期防护（ROUND6 补漏）：已确认公司行动的生效日若尚未在本账户
        # 处理（如生效日因 pending 暂停、隔日确认后直接运行后续日期），禁止
        # 带着旧股数恢复运行——否则真实价相对旧成本立刻产生虚假亏损和错误止损。
        missed = self._find_missed_effective_date(
            account_id, account, date, baseline, corporate_actions)
        if missed is not None:
            return self._build_paused_result(account_id, date, baseline, missed)

        # 5.5 盘前公司行动处理（真实价格架构 v3 + ROUND3 确认闸门 + ROUND5 安全处理）
        # confirmed 生效日：调整 shares/cost/last_price；pending 涉持仓：暂停账户运行
        # （不生成交易、不估值、不保存当日状态，待确认后重跑）；pending 未持仓：
        # 该标的当日禁止新买入（直到记录被确认为止）。
        pause_info = None
        suspended_buy: set = set()
        if corporate_actions:
            baseline, pause_info, suspended_buy = self._apply_corporate_actions(
                baseline, date, corporate_actions)
        if pause_info is not None:
            return self._build_paused_result(account_id, date, baseline, pause_info)

        trades: List[Dict] = []
        skipped: List[Dict] = []
        orders: List[Dict] = []
        signals: List[Dict] = []

        # 5.5 步之前：解析账户执行配置（止损线、调仓日、防御开关、标的池等均以此为准）。
        # 执行口径门闩（2026-10-22 复审补修 ④）：老账户（创建时无 config_version）
        # 在用户显式确认前不得按新行为运行——返回暂停结果，明确提示确认入口。
        account_cfg = resolve_execution_config(account)
        if account_cfg is None:
            return self._build_paused_result(
                account_id, date, baseline, {"kind": "behavior_ack_required"})

        # 6. 执行今天到期的调仓信号
        pending_signals = self.service.store.load_pending_signals(account_id, date)
        for signal in pending_signals:
            if is_shadow:
                signal_orders, signal_skipped = self._build_signal_orders(
                    account_id, date, baseline, signal, open_prices,
                    blocked_buy_tickers=suspended_buy)
                orders.extend(signal_orders)
                skipped.extend(signal_skipped)
            else:
                signal_trades, signal_skipped, signal_orders = self._execute_signal(
                    account_id, date, baseline, signal, open_prices,
                    blocked_buy_tickers=suspended_buy)
                trades.extend(signal_trades)
                skipped.extend(signal_skipped)
                orders.extend(signal_orders)
                baseline = self._apply_trades_to_state(baseline, signal_trades, open_prices)

        # 7. 止损检查与执行（使用开盘价；止损线取账户冻结配置，缺省 -0.08）
        sl_orders, sl_skipped = self._check_stop_loss(
            account_id, date, baseline, open_prices,
            stop_loss=float(account_cfg.get("stop_loss", -0.08)))
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

        # 8. 每日估值（使用收盘价，只更新市值）
        baseline = self._apply_valuation(baseline, close_prices)

        # 9. 如果是调仓日（取账户冻结配置的调仓日，缺省周四），准备信号供下一交易日执行
        if self.calendar.is_rebalance_day(
                date, int(account_cfg.get("rebalance_weekday", 3))) and scores_df is not None:
            # 复审补修 ①：评分字段缺失/无效当日即失败，不保存将无法执行的信号
            import config as config_module
            from .entry_filter import validate_score_data
            _pool = set(account_cfg.get("industry_tickers")
                        or config_module.ETF_UNIVERSE.keys()) | set(
                        account_cfg.get("defense_tickers")
                        or config_module.DEFENSE_UNIVERSE.keys())
            validate_score_data(scores_df, _pool, context="虚拟盘信号准备")
            cfg = account_cfg
            next_day = self.calendar.next_trading_day(date)
            signals.append(self._prepare_rebalance_signal(account_id, date, next_day, scores_df, cfg))

        # 10. 原子保存最终状态
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

    def _apply_corporate_actions(self, state, date, actions):
        """盘前公司行动处理（真实价格架构 v3 + 确认闸门 + ROUND5 安全处理）。

        份额拆分生效日（effective_date == date 且 confirmed）：shares×=ratio、
        cost_price÷=ratio、last_price÷=ratio。市值与总资产不变——不腰斩、不虚增；
        成本价同比例调整使止损判断不受拆分影响。

        确认闸门（硬规则）：
        - 仅 status == 'confirmed'（基金正式公告核实）的记录可调整持仓；
        - status 缺省按 pending 处理（老调用方兼容 = 不自动执行）。

        ROUND5 安全处理（pending 且 effective_date <= date）：
        - 涉及当前持仓（含 ratio 非法无法确认股数变化）：返回 pause_info，
          run_daily 据此暂停整个账户运行——不生成交易、不估值、不保存当日
          状态（当日保持未处理，确认后重跑同一日即可）；
        - 未持有该标的：加入 suspended_buy，本次运行禁止其新买入订单
          （直到记录被确认为止，跨交易日持续）。

        幂等：同一只股票同一天的行动只应用一次；重复传入相同 actions
        不会产生二次调整。返回 (state, pause_info, suspended_buy)。
        """
        pause_info = None
        suspended_buy = set()
        applied = set()
        for action in actions:
            if action.get("action_type") not in (None, "split"):
                continue  # 目前仅支持份额拆分；其他行动类型显式跳过
            ticker = action.get("ticker")
            eff = str(action.get("effective_date", ""))[:10]
            status = action.get("status") or "pending"
            if status != "confirmed":
                if eff > date:
                    continue  # 未来生效的待确认记录暂不处理
                pos = state["positions"].get(ticker)
                if pos is not None:
                    if pause_info is None:
                        pause_info = {
                            "ticker": ticker, "effective_date": eff,
                            "note": action.get("note") or action.get("action_type"),
                        }
                else:
                    suspended_buy.add(ticker)
                continue
            if eff != date:
                continue  # confirmed 仅在生效日调整
            pos = state["positions"].get(ticker)
            if pos is None:
                continue
            if ticker in applied:
                continue  # 同标的同日复式记录只应用一次
            try:
                ratio = float(action.get("ratio") or 1.0)
            except (TypeError, ValueError):
                # confirmed 但比例非法：股数变化无法确认，按涉持仓安全暂停
                pause_info = {
                    "ticker": ticker, "effective_date": eff,
                    "note": f"invalid ratio: {action.get('ratio')!r}",
                }
                continue
            if ratio <= 0 or abs(ratio - 1.0) < 1e-12:
                continue
            pos["shares"] = pos["shares"] * ratio
            pos["cost_price"] = pos["cost_price"] / ratio
            pos["last_price"] = pos["last_price"] / ratio
            applied.add(ticker)
        return state, pause_info, suspended_buy

    def _find_missed_effective_date(self, account_id, account, date, state, actions):
        """跳期防护（ROUND6 补漏①）：已确认公司行动的生效日若尚未在本账户
        处理（当日无已保存状态），禁止直接运行后续日期。

        场景：生效日因 pending 暂停 → 隔日确认 → 若直接运行隔日，基线仍是
        生效前旧股数，面对拆后真实价会立刻产生虚假亏损与错误止损。必须先
        补跑生效日（确认后重跑该日会正常调整股数），再续跑后续日期。

        只在持仓该标的时阻止；生效日早于账户起点不阻止（起点持仓为人工
        导入口径）；当日（date == 生效日）不阻止（5.5 步正常调整）。
        返回 missed 信息 dict 或 None。
        """
        if not actions:
            return None
        start = str(account.get("start_date", ""))[:10]
        for action in actions:
            if action.get("action_type") not in (None, "split"):
                continue
            if (action.get("status") or "pending") != "confirmed":
                continue
            ticker = action.get("ticker")
            if ticker not in state["positions"]:
                continue
            eff = str(action.get("effective_date", ""))[:10]
            if not eff or eff >= date:
                continue
            if start and eff < start:
                continue
            if self.service.store.is_day_processed(account_id, eff):
                continue
            return {"kind": "missed_effective_date", "ticker": ticker,
                    "effective_date": eff}
        return None

    def _build_paused_result(self, account_id, date, baseline, pause_info):
        """安全暂停结果：明确提示原因，不包含任何交易/估值/保存动作。"""
        if pause_info.get("kind") == "behavior_ack_required":
            reason = (
                "账户运行已暂停：该账户创建于执行口径一致性修复之前（旧行为："
                "仅按总分过滤、标的池跟随侧栏全局变量）。为落实完整 B0.4 入场"
                "条件（趋势/确认/弱市55分门槛/收盘在MA20之上/均线斜率/数据成熟期）"
                "并冻结标的池，需要您在页面「账户操作」中显式确认按新口径运行；"
                "确认时将记录生效日期并冻结确认时刻的标的池快照。"
                "未确认前不生成任何交易、不保存当日状态。"
            )
        elif pause_info.get("kind") == "missed_effective_date":
            reason = (
                f"账户运行已暂停：{pause_info['ticker']} 的公司行动生效日 "
                f"{pause_info['effective_date']} 尚未在本账户处理（当日未运行"
                f"或曾暂停）。若直接续跑将带着旧股数面对拆后价格，产生虚假"
                f"亏损与错误止损。请先补跑 {pause_info['effective_date']} "
                f"（确认后重跑该日会正常调整股数），再运行 {date}。"
            )
        else:
            reason = (
                f"账户运行已暂停：{pause_info['ticker']} 存在待确认公司行动"
                f"（生效日 {pause_info['effective_date']}，{pause_info['note']}），"
                f"股数变化未经基金公告确认。请核实公告并置为 confirmed 后重跑本日。"
            )
        return {
            "paused": True,
            "date": date,
            "account_id": account_id,
            "reason": reason,
            "pending_action": pause_info,
            "cash": baseline["cash"],
            "positions": baseline["positions"],
            "nav": baseline["cash"] + sum(
                p["shares"] * p["last_price"] for p in baseline["positions"].values()
            ),
            "trades": [],
            "skipped": [],
            "orders": [],
            "signals": [],
        }

    def _apply_valuation(self, state, prices):
        """更新持仓市值（不修改股数）。缺少价格时保留最近有效价格。"""
        for ticker, pos in state["positions"].items():
            price = prices.get(ticker, pos["last_price"])
            if price is None or price <= 0:
                continue
            pos["last_price"] = price
        return state

    def _check_stop_loss(self, account_id, trade_date, state, prices,
                         stop_loss=-0.08, skip_tickers=None):
        """检查持仓是否触发止损。缺少开盘价的记录未执行原因。

        skip_tickers: 当日有待确认公司行动的标的集合，跳过止损检查
        （价差由未经确认的份额变动推测而来，不得据此自动卖出）。
        """
        orders = []
        skipped = []
        skip = skip_tickers or set()
        now = datetime.now().isoformat(timespec="seconds")
        for ticker, pos in state["positions"].items():
            if ticker in skip:
                skipped.append({
                    "order_id": None,
                    "ticker": ticker,
                    "action": "STOP_LOSS",
                    "reason": f"skipped: pending corporate action for {ticker}",
                })
                continue
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

    def _build_signal_orders(self, account_id, trade_date, baseline, signal, open_prices,
                             blocked_buy_tickers=None):
        """生成调仓信号对应的 PENDING 订单（用于影子账户，不执行）。

        blocked_buy_tickers：存在待确认公司行动、暂停新买入资格的标的集合；
        其 BUY 计划被剔除并留痕（ROUND5 安全处理）。
        """
        from io import StringIO
        scores_df = pd.read_json(StringIO(signal["scores_json"]))
        signal_date = signal["signal_date"]
        now = datetime.now().isoformat(timespec="seconds")

        cfg = resolve_execution_config(self.service.store.get_account(account_id))
        if cfg is None:
            raise ValueError(
                f"账户 {account_id} 尚未确认新执行口径，拒绝生成调仓订单；"
                f"请先在页面「账户操作」中确认。"
            )
        planner_trades, _ = self._run_planner(baseline, scores_df, open_prices, cfg)

        blocked = blocked_buy_tickers or set()
        orders = []
        blocked_skips = []
        for trade in planner_trades:
            action = trade["action"]
            ticker = trade["ticker"]
            if action == "BUY" and ticker in blocked:
                blocked_skips.append({
                    "order_id": None, "ticker": ticker, "action": action,
                    "reason": (f"new buy suspended: pending corporate action "
                               f"for {ticker}, awaiting confirmation"),
                })
                continue
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
        return orders, blocked_skips

    def _execute_signal(self, account_id, trade_date, baseline, signal, open_prices,
                        blocked_buy_tickers=None):
        """执行保存的信号：强制使用账户创建时保存的冻结配置。"""
        orders, blocked_skips = self._build_signal_orders(
            account_id, trade_date, baseline, signal, open_prices,
            blocked_buy_tickers=blocked_buy_tickers)
        trades, skipped, orders_out = self._execute_orders_in_memory(
            account_id, trade_date, baseline, orders, open_prices)
        return trades, blocked_skips + skipped, orders_out

    # ============== Rebalance Planner ==============

    def _run_planner(self, state, scores_df, prices, cfg=None):
        """使用 plan_rebalance_v2_5 生成调仓计划。NAV 使用当日开盘价重新计算。

        账户设置一致性修复（2026-10-22 + 复审补修）：
        - 标的池优先使用账户创建时冻结的 industry_tickers/defense_tickers
          （仅直接调用/测试的兼容路径回退 config 模块当前值；经 run_daily
          进入的账户已由执行口径门闩保证持有冻结池）；
        - defense_enabled 取账户冻结配置（缺省 True，与旧行为一致）；
        - 入场过滤走共享模块 entry_filter.select_candidates，与正式 B0.4
          生产信号路径（generate_signals + _rebalance_v2 二次过滤）逐条对齐：
          弱市55分门槛、防御资格宽松档、prev_close>ma20、ma20_slope、
          数据成熟期 history_count>=51；评分字段缺失或无效时抛出
          ValueError 停止生成交易（不再有"缺列退回仅看总分"的宽松路径）。
        """
        import config as config_module

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

        # 标的池：账户创建时冻结优先，回退 config 模块（直接调用/测试兼容路径）
        industry_tickers = set(account_cfg.get("industry_tickers")
                               or config_module.ETF_UNIVERSE.keys())
        defense_tickers = set(account_cfg.get("defense_tickers")
                              or config_module.DEFENSE_UNIVERSE.keys())

        # B0.4 完整入场条件（缺字段/无效字段在此抛出 ValueError）
        industry_candidates, defense_candidates, _meta = select_candidates(
            scores_df,
            industry_tickers=industry_tickers,
            defense_tickers=defense_tickers,
            min_total_score=account_cfg.get("min_total_score", 40),
            min_trend_score=account_cfg.get("min_trend_score", 15),
            min_confirm_score=account_cfg.get("min_confirm_score", 4),
            context="虚拟盘调仓",
        )

        price_map = {}
        for t in industry_tickers | defense_tickers:
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
            industry_tickers=industry_tickers,
            defense_tickers=defense_tickers,
            max_industry_holdings=account_cfg.get("max_holdings", 5),
            max_defense_holdings=2,
            max_total_holdings=account_cfg.get("max_holdings", 5),
            max_position_per_etf=account_cfg.get("max_position_per_etf", 0.20),
            max_total_position=1.0,
            commission_rate=self.COMMISSION_RATE,
            min_commission=self.MIN_COMMISSION,
            lot_size=self.LOT_SIZE,
            defense_enabled=account_cfg.get("defense_enabled", True),
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
