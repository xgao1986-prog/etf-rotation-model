#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实盘交互与信号发布模块 v0.1

LiveTradingAssistant: 连接真实持仓与 B0.4 模型信号

核心原则：
- 真实持仓以用户录入为准
- 模型只生成目标组合和交易建议
- 实际成交后用户录入，系统更新真实持仓
- 不自动下单，不修改 B0.4 策略规则
"""

import os, csv, json, warnings
from dataclasses import dataclass, field
from typing import List, Dict, Optional
from datetime import datetime
import pandas as pd
import numpy as np

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
CASH_TICKER = "__CASH__"
CASH_NAME = "现金"
SHARE_UNIT = 100
DEFAULT_LIVE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "live")
DEFAULT_REPORTS_DIR = os.path.join(os.path.dirname(__file__), "..", "reports", "live")

# 18只ETF池（B0.4 核心池 + 防御池）
from config import ETF_UNIVERSE, DEFENSE_UNIVERSE
ALLOWED_TICKERS = set(list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys()) + [CASH_TICKER])

# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------
@dataclass
class ValidationReport:
    """持仓校验报告"""
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    ok: bool = True

    def add_error(self, msg: str):
        self.errors.append(msg)
        self.ok = False

    def add_warning(self, msg: str):
        self.warnings.append(msg)


@dataclass
class TradePlanRow:
    """交易计划行"""
    ticker: str
    action: str  # BUY / SELL / HOLD
    current_shares: int
    target_shares: int
    delta_shares: int
    estimated_price: float
    estimated_amount: float
    reason: str
    commission: float
    post_cash: float


@dataclass
class ActualTrade:
    """实际成交记录"""
    date: str
    ticker: str
    action: str
    shares: int
    actual_price: float
    commission: float
    note: str = ""


# ---------------------------------------------------------------------------
# 核心类
# ---------------------------------------------------------------------------
class LiveTradingAssistant:
    """
    实盘交互与信号发布模块 v0.1

    文件路径：
    - positions_path: data/live/actual_positions.csv
    - trades_path:    data/live/actual_trades.csv
    - plan_path:      data/live/latest_trade_plan.csv
    """

    def __init__(self, positions_path=None, trades_path=None, plan_path=None,
                 config=None, commission_rate=0.0003, min_commission=0.1):
        self.positions_path = positions_path or os.path.join(DEFAULT_LIVE_DIR, "actual_positions.csv")
        self.trades_path = trades_path or os.path.join(DEFAULT_LIVE_DIR, "actual_trades.csv")
        self.plan_path = plan_path or os.path.join(DEFAULT_LIVE_DIR, "latest_trade_plan.csv")
        self.config = config or {}
        self.commission_rate = commission_rate
        self.min_commission = min_commission
        os.makedirs(os.path.dirname(self.positions_path), exist_ok=True)
        os.makedirs(os.path.dirname(self.trades_path), exist_ok=True)
        os.makedirs(os.path.dirname(self.plan_path), exist_ok=True)
        os.makedirs(DEFAULT_REPORTS_DIR, exist_ok=True)

    # ------------------------------------------------------------------
    # 持仓管理
    # ------------------------------------------------------------------
    def load_positions(self) -> pd.DataFrame:
        """读取真实持仓 CSV。如果文件不存在，返回空模板。"""
        if not os.path.exists(self.positions_path):
            return pd.DataFrame(columns=[
                "ticker", "name", "shares", "cost_price", "current_price",
                "market_value", "available_cash", "update_time"
            ])
        df = pd.read_csv(self.positions_path)
        # 确保列存在
        for col in ["ticker", "name", "shares", "cost_price", "current_price",
                    "market_value", "available_cash", "update_time"]:
            if col not in df.columns:
                df[col] = 0.0 if col != "update_time" else ""
                if col in ["ticker", "name", "update_time"]:
                    df[col] = ""
        return df

    def save_positions(self, df: pd.DataFrame):
        """保存真实持仓 CSV。"""
        df.to_csv(self.positions_path, index=False)

    # ------------------------------------------------------------------
    # 校验逻辑
    # ------------------------------------------------------------------
    def validate_positions(self, df: pd.DataFrame = None) -> ValidationReport:
        """
        校验持仓数据。

        规则：
        1. ticker 必须在 ETF_UNIVERSE 或 DEFENSE_UNIVERSE 中（除 CASH_TICKER）
        2. shares 必须是 100 的整数倍（非现金）
        3. 现金 + 持仓市值 = 总资产（NAV恒等式）
        4. current_price > 0（非现金）
        5. 总仓位检查
        """
        report = ValidationReport()
        if df is None:
            df = self.load_positions()

        if df.empty:
            report.add_warning("持仓为空，请先录入持仓")
            return report

        # 分离现金和持仓
        cash_row = df[df["ticker"] == CASH_TICKER]
        holdings = df[df["ticker"] != CASH_TICKER]

        # 1. ETF 池检查
        for _, row in holdings.iterrows():
            if row["ticker"] not in ALLOWED_TICKERS:
                report.add_error(
                    f"模型外持仓: {row['ticker']} ({row['name']}) 不在 B0-18 池内"
                )

        # 2. 100股整数检查
        for _, row in holdings.iterrows():
            if row["shares"] % SHARE_UNIT != 0:
                report.add_warning(
                    f"非100股整数: {row['ticker']} shares={row['shares']}，建议调整为 {row['shares'] // SHARE_UNIT * SHARE_UNIT}"
                )

        # 3. 缺价检查
        for _, row in holdings.iterrows():
            if pd.isna(row["current_price"]) or row["current_price"] <= 0:
                report.add_error(f"缺价: {row['ticker']} current_price={row['current_price']}")

        # 4. NAV 恒等式检查
        if not cash_row.empty:
            cash = float(cash_row.iloc[0]["market_value"])
        else:
            cash = 0.0
            report.add_warning("缺少现金行，请添加 __CASH__")

        total_mv = holdings["market_value"].sum()
        # 尝试从 available_cash 列读取总资产（如果存在）
        total_asset = cash + total_mv
        if not cash_row.empty and "available_cash" in cash_row.columns:
            user_total = float(cash_row.iloc[0].get("available_cash", 0))
            if user_total > 0 and abs(user_total - total_asset) > 1.0:
                report.add_error(
                    f"NAV恒等式不成立: 现金({cash:.2f}) + 市值({total_mv:.2f}) = {total_asset:.2f} "
                    f"≠ 用户总资产({user_total:.2f})"
                )

        # 5. 总仓位检查（如果 config 中有 max_total_position）
        max_pos = self.config.get("max_total_position", 1.0)
        if total_mv > 0 and cash >= 0:
            position_ratio = total_mv / (total_mv + cash)
            if position_ratio > max_pos + 0.01:
                report.add_warning(
                    f"总仓位超限: {position_ratio:.1%} > 上限 {max_pos:.1%}"
                )

        return report

    # ------------------------------------------------------------------
    # 价格更新
    # ------------------------------------------------------------------
    def update_prices(self, price_map: Dict[str, float], date: str = None) -> pd.DataFrame:
        """
        更新持仓价格。

        price_map: {ticker: current_price}
        date: 更新日期，默认今天
        """
        df = self.load_positions()
        if df.empty:
            return df

        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        for idx, row in df.iterrows():
            ticker = row["ticker"]
            if ticker == CASH_TICKER:
                continue
            if ticker in price_map:
                df.at[idx, "current_price"] = price_map[ticker]
                df.at[idx, "market_value"] = price_map[ticker] * row["shares"]
                df.at[idx, "update_time"] = date

        self.save_positions(df)
        return df

    # ------------------------------------------------------------------
    # 止损检查
    # ------------------------------------------------------------------
    def check_stop_loss(self, df: pd.DataFrame = None,
                        stop_loss_pct: float = None) -> pd.DataFrame:
        """
        检查止损。

        返回触发止损的持仓 DataFrame。
        """
        if df is None:
            df = self.load_positions()
        if df.empty:
            return pd.DataFrame()

        if stop_loss_pct is None:
            stop_loss_pct = self.config.get("stop_loss", 0.08)

        holdings = df[df["ticker"] != CASH_TICKER].copy()
        if holdings.empty:
            return pd.DataFrame()

        holdings["loss_pct"] = (holdings["current_price"] - holdings["cost_price"]) / holdings["cost_price"]
        alerts = holdings[holdings["loss_pct"] < -stop_loss_pct].copy()
        if not alerts.empty:
            alerts["alert_reason"] = f"跌破止损线({stop_loss_pct:.0%})"
        return alerts

    # ------------------------------------------------------------------
    # 订单生成
    # ------------------------------------------------------------------
    def generate_trade_plan(self, target_positions: Dict[str, int],
                            price_map: Dict[str, float],
                            date: str = None) -> pd.DataFrame:
        """
        生成交易计划。

        target_positions: {ticker: target_shares}
        price_map: {ticker: estimated_price}

        返回 DataFrame: ticker, action, current_shares, target_shares, delta_shares,
                       estimated_price, estimated_amount, reason, commission, post_cash
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        actual_df = self.load_positions()
        actual = {r["ticker"]: int(r["shares"]) for _, r in actual_df.iterrows() if r["ticker"] != CASH_TICKER}

        # 现金
        cash = 0.0
        cash_rows = actual_df[actual_df["ticker"] == CASH_TICKER]
        if not cash_rows.empty:
            cash = float(cash_rows.iloc[0]["market_value"])

        # 合并所有涉及的 ticker
        all_tickers = set(list(actual.keys()) + list(target_positions.keys()))

        rows = []
        sell_orders = []
        buy_orders = []

        for ticker in all_tickers:
            curr = actual.get(ticker, 0)
            target = target_positions.get(ticker, 0)
            delta = target - curr

            if delta == 0:
                if curr > 0:
                    rows.append({
                        "ticker": ticker,
                        "action": "HOLD",
                        "current_shares": curr,
                        "target_shares": target,
                        "delta_shares": 0,
                        "estimated_price": price_map.get(ticker, 0),
                        "estimated_amount": 0.0,
                        "reason": "保留",
                        "commission": 0.0,
                        "post_cash": cash,
                    })
                continue

            price = price_map.get(ticker, 0)

            if price <= 0:
                # 缺价格，不能生成有效订单
                rows.append({
                    "ticker": ticker,
                    "action": "BUY" if delta > 0 else "SELL",
                    "current_shares": curr,
                    "target_shares": target,
                    "delta_shares": delta,
                    "estimated_price": 0,
                    "estimated_amount": 0.0,
                    "reason": "缺价格，不能生成有效订单（请更新行情）",
                    "commission": 0.0,
                    "post_cash": cash,
                })
                continue

            amount = abs(delta) * price
            commission = max(amount * self.commission_rate, self.min_commission)

            if delta > 0:
                buy_orders.append({
                    "ticker": ticker,
                    "action": "BUY",
                    "current_shares": curr,
                    "target_shares": target,
                    "delta_shares": delta,
                    "estimated_price": price,
                    "estimated_amount": amount,
                    "reason": "调仓买入",
                    "commission": commission,
                })
            else:
                sell_orders.append({
                    "ticker": ticker,
                    "action": "SELL",
                    "current_shares": curr,
                    "target_shares": target,
                    "delta_shares": delta,
                    "estimated_price": price,
                    "estimated_amount": amount,
                    "reason": "调仓卖出",
                    "commission": commission,
                })

        # 先执行 SELL（释放现金），再执行 BUY
        post_cash = cash
        for o in sell_orders:
            post_cash += o["estimated_amount"] - o["commission"]
            o["post_cash"] = post_cash
            rows.append(o)

        for o in buy_orders:
            need = o["estimated_amount"] + o["commission"]
            if post_cash >= need:
                post_cash -= need
                o["post_cash"] = post_cash
                rows.append(o)
            else:
                # 现金不足：缩放订单或标记
                o["reason"] = f"调仓买入（现金不足，需 {need:.2f}，可用 {post_cash:.2f}）"
                o["post_cash"] = post_cash
                rows.append(o)

        plan_df = pd.DataFrame(rows, columns=[
            "ticker", "action", "current_shares", "target_shares", "delta_shares",
            "estimated_price", "estimated_amount", "reason", "commission", "post_cash"
        ])
        plan_df.to_csv(self.plan_path, index=False)
        return plan_df

    # ------------------------------------------------------------------
    # 成交记录
    # ------------------------------------------------------------------
    def record_trade(self, trade: ActualTrade) -> pd.DataFrame:
        """记录实际成交到 trades CSV。"""
        rows = []
        if os.path.exists(self.trades_path):
            with open(self.trades_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)

        rows.append({
            "date": trade.date,
            "ticker": trade.ticker,
            "action": trade.action,
            "shares": trade.shares,
            "actual_price": trade.actual_price,
            "commission": trade.commission,
            "note": trade.note,
        })

        with open(self.trades_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "date", "ticker", "action", "shares", "actual_price", "commission", "note"
            ])
            writer.writeheader()
            writer.writerows(rows)

        return pd.DataFrame(rows)

    def apply_trade(self, trade: ActualTrade) -> pd.DataFrame:
        """
        用实际成交更新真实持仓。

        注意：这是实际成交后的更新，不是模型建议。
        """
        df = self.load_positions()

        # 找到对应 ticker
        mask = df["ticker"] == trade.ticker
        if not mask.any():
            # 新建持仓行
            new_row = {
                "ticker": trade.ticker,
                "name": "",
                "shares": trade.shares if trade.action == "BUY" else 0,
                "cost_price": trade.actual_price,
                "current_price": trade.actual_price,
                "market_value": trade.shares * trade.actual_price if trade.action == "BUY" else 0,
                "available_cash": 0,
                "update_time": trade.date,
            }
            df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
        else:
            idx = df[mask].index[0]
            curr_shares = int(df.at[idx, "shares"])
            if trade.action == "BUY":
                # 更新成本价（加权平均）
                old_cost = float(df.at[idx, "cost_price"])
                old_mv = curr_shares * old_cost
                new_mv = trade.shares * trade.actual_price
                total_shares = curr_shares + trade.shares
                avg_cost = (old_mv + new_mv) / total_shares if total_shares > 0 else trade.actual_price
                df.at[idx, "shares"] = total_shares
                df.at[idx, "cost_price"] = avg_cost
                df.at[idx, "current_price"] = trade.actual_price
                df.at[idx, "market_value"] = total_shares * trade.actual_price
            elif trade.action == "SELL":
                new_shares = max(0, curr_shares - trade.shares)
                df.at[idx, "shares"] = new_shares
                df.at[idx, "current_price"] = trade.actual_price
                df.at[idx, "market_value"] = new_shares * trade.actual_price
            df.at[idx, "update_time"] = trade.date

        # 更新现金
        cash_mask = df["ticker"] == CASH_TICKER
        if cash_mask.any():
            cash_idx = df[cash_mask].index[0]
            curr_cash = float(df.at[cash_idx, "market_value"])
            if trade.action == "BUY":
                curr_cash -= trade.shares * trade.actual_price + trade.commission
            elif trade.action == "SELL":
                curr_cash += trade.shares * trade.actual_price - trade.commission
            df.at[cash_idx, "market_value"] = curr_cash
            df.at[cash_idx, "update_time"] = trade.date

        self.save_positions(df)
        # 同时记录到成交记录
        self.record_trade(trade)
        return df

    # ------------------------------------------------------------------
    # 报告生成
    # ------------------------------------------------------------------
    def generate_daily_alert(self, date: str = None, output_path: str = None) -> str:
        """生成每日止损检查报告。"""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        if output_path is None:
            output_path = os.path.join(DEFAULT_REPORTS_DIR, f"daily_stop_loss_alert_{date}.md")
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        alerts = self.check_stop_loss()
        lines = []
        lines.append(f"# 每日止损检查报告 ({date})")
        lines.append("")

        if alerts.empty:
            lines.append("✅ **今日无触发止损的持仓。**")
        else:
            lines.append(f"⚠️ **今日触发 {len(alerts)} 只持仓止损：**")
            lines.append("")
            lines.append("| ticker | 名称 | 股数 | 成本价 | 现价 | 亏损幅度 | 建议 |")
            lines.append("|--------|------|------|--------|------|----------|------|")
            for _, r in alerts.iterrows():
                lines.append(
                    f"| {r['ticker']} | {r['name']} | {r['shares']} | "
                    f"{r['cost_price']:.3f} | {r['current_price']:.3f} | "
                    f"{r['loss_pct']:.2%} | 建议卖出 |"
                )

        lines.append("")
        lines.append("> 本报告由 B0.4 实盘助手生成，仅供参考。实际交易需用户手动确认。")

        content = "\n".join(lines)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)
        return content

    def generate_weekly_plan(self, plan_df: pd.DataFrame = None, date: str = None,
                             output_path: str = None) -> str:
        """生成每周调仓计划报告。"""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        if output_path is None:
            output_path = os.path.join(DEFAULT_REPORTS_DIR, f"weekly_rebalance_plan_{date}.md")
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        if plan_df is None:
            if os.path.exists(self.plan_path):
                plan_df = pd.read_csv(self.plan_path)
            else:
                plan_df = pd.DataFrame()

        lines = []
        lines.append(f"# 每周调仓计划 ({date})")
        lines.append("")

        if plan_df.empty:
            lines.append("✅ **本周无调仓建议，持仓保持不变。**")
        else:
            buy_df = plan_df[plan_df["action"] == "BUY"]
            sell_df = plan_df[plan_df["action"] == "SELL"]
            hold_df = plan_df[plan_df["action"] == "HOLD"]
            cash_after = plan_df["post_cash"].iloc[-1] if not plan_df.empty else 0

            lines.append(f"- **买入订单**: {len(buy_df)} 笔")
            lines.append(f"- **卖出订单**: {len(sell_df)} 笔")
            lines.append(f"- **保留持仓**: {len(hold_df)} 笔")
            lines.append(f"- **预计剩余现金**: {cash_after:,.2f}")
            lines.append("")

            if not buy_df.empty or not sell_df.empty:
                lines.append("## 建议订单列表")
                lines.append("")
                lines.append("| 操作 | ticker | 当前股数 | 目标股数 | 差额 | 预计价格 | 预计金额 | 佣金 | 原因 |")
                lines.append("|------|--------|----------|----------|------|----------|----------|------|------|")
                for _, r in plan_df.iterrows():
                    if r["action"] == "HOLD":
                        continue
                    lines.append(
                        f"| {r['action']} | {r['ticker']} | {r['current_shares']} | "
                        f"{r['target_shares']} | {r['delta_shares']} | "
                        f"{r['estimated_price']:.3f} | {r['estimated_amount']:,.2f} | "
                        f"{r['commission']:.2f} | {r['reason']} |"
                    )
                lines.append("")

            if not hold_df.empty:
                lines.append("## 保留持仓")
                lines.append("")
                for _, r in hold_df.iterrows():
                    lines.append(f"- {r['ticker']}: {r['current_shares']} 股（保持不变）")
                lines.append("")

            # 检查缺价格订单
            missing_price = plan_df[plan_df["reason"].str.contains("缺价格", na=False)]
            if not missing_price.empty:
                lines.append("## ⚠️ 缺价格警告")
                lines.append("")
                lines.append("以下订单因缺少价格无法生成有效预估，请运行 `py scripts/live_update_positions.py` 更新行情后重新生成：")
                lines.append("")
                for _, r in missing_price.iterrows():
                    lines.append(f"- {r['action']} {r['ticker']}: 目标股数 {r['target_shares']}，但 estimated_price=0")
                lines.append("")

        lines.append("> ⚠️ **注意**：本计划由 B0.4 模型生成，实际成交需用户手动确认并录入。")
        lines.append("> 不自动下单，不保证建议被完全执行。")

        content = "\n".join(lines)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)
        return content


# ---------------------------------------------------------------------------
# 辅助函数：创建示例持仓文件
# ---------------------------------------------------------------------------
def create_sample_positions(path: str, tickers: list = None, cash: float = 150000.0):
    """创建示例持仓文件，用于测试。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rows = []
    if tickers:
        for t in tickers:
            rows.append({
                "ticker": t,
                "name": "",
                "shares": 200,
                "cost_price": 1.0,
                "current_price": 1.05,
                "market_value": 210.0,
                "available_cash": 0,
                "update_time": "2026-06-26",
            })
    rows.append({
        "ticker": CASH_TICKER,
        "name": CASH_NAME,
        "shares": 0,
        "cost_price": 0,
        "current_price": 0,
        "market_value": cash,
        "available_cash": cash + sum([r["market_value"] for r in rows if r["ticker"] != CASH_TICKER]),
        "update_time": "2026-06-26",
    })
    pd.DataFrame(rows).to_csv(path, index=False)
