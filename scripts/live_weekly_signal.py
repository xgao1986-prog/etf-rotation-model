#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Live Weekly Signal — v0.2

每周运行（默认周四收盘后）：
1. 更新 B0.4 正式池数据
2. 运行 B0.4 信号生成
3. 基于真实持仓生成交易计划
4. 追加纸面交易日志
5. 输出每周调仓信号报告

用法：
    py scripts/live_weekly_signal.py --date 2026-06-26

注意：
- 只输出建议，不自动下单
- T日收盘信号，T+1开盘为建议成交口径
- 数据不完整时给出警告，不生成交易建议

输出：
    data/live/latest_trade_plan.csv
    data/live/paper_trading_log.csv
    reports/live/latest_weekly_signal.md
"""

import argparse, os, sys, warnings
from datetime import datetime, timedelta
import pandas as pd

sys.path.insert(0, "src")

from config import build_config, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK
from database import ETFDatabase
from live_trading_assistant import LiveTradingAssistant


# 正式池
FORMAL_POOL = list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys()) + [BENCHMARK]

DATA_LIVE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "live")
REPORTS_LIVE_DIR = os.path.join(os.path.dirname(__file__), "..", "reports", "live")


def ensure_dirs():
    os.makedirs(DATA_LIVE_DIR, exist_ok=True)
    os.makedirs(REPORTS_LIVE_DIR, exist_ok=True)


def check_data_completeness(db, date, tickers):
    """检查数据完整性。"""
    end_date = datetime.strptime(date, "%Y-%m-%d")
    start_date = end_date - timedelta(days=30)
    start_str = start_date.strftime("%Y-%m-%d")
    market_df = db.get_market_data(ticker=tickers, start_date=start_str, end_date=date)

    missing = []
    for ticker in tickers:
        t_data = market_df[market_df["ticker"] == ticker]
        if t_data.empty:
            missing.append(ticker)
        else:
            last_date = t_data["date"].max()
            if last_date < pd.Timestamp(date) - pd.Timedelta(days=7):
                missing.append(ticker)

    return len(missing) == 0, missing


def get_b0_4_signals(assistant, date, cfg):
    """运行 B0.4 信号，返回目标持仓。

    复用 live_generate_trade_plan.py 的逻辑。
    """
    db = ETFDatabase()
    tickers = list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())

    market_df = db.get_market_data(ticker=tickers)
    if market_df.empty:
        return {}, {}

    from strategy import StrategyEngine
    engine = StrategyEngine(cfg)
    signals = engine.generate_signals(market_df)
    if signals.empty:
        return {}, {}

    latest = signals[signals["date"] == signals["date"].max()].copy()
    latest = latest[latest["qualified"]].sort_values("total_score", ascending=False)

    max_holdings = cfg.get("max_holdings", 5)
    selected = latest.head(max_holdings)

    positions_df = assistant.load_positions()
    cash_rows = positions_df[positions_df["ticker"] == "__CASH__"]
    total_asset = float(cash_rows.iloc[0]["market_value"]) if not cash_rows.empty else 0.0
    for _, r in positions_df.iterrows():
        if r["ticker"] != "__CASH__" and r["current_price"] > 0:
            total_asset += r["market_value"]

    max_position = cfg.get("max_position_per_etf", 0.20)
    target_amount = total_asset * max_position

    price_map = {}
    for t in selected["ticker"].unique():
        try:
            data = db.get_market_data(ticker=t, start_date=date, end_date=date)
            if not data.empty:
                price_map[t] = data["close"].iloc[-1]
        except Exception:
            pass

    target_positions = {}
    for _, row in selected.iterrows():
        t = row["ticker"]
        price = price_map.get(t, 0)
        if price > 0 and target_amount > 0:
            shares = int(target_amount / price // 100 * 100)
            target_positions[t] = shares
        else:
            target_positions[t] = 0

    return target_positions, price_map


def append_paper_log(signal_date, suggested_trade_date, ticker, action,
                     suggested_shares, suggested_price, model_reason):
    """追加纸面交易日志。"""
    log_path = os.path.join(DATA_LIVE_DIR, "paper_trading_log.csv")
    plan_id = f"{signal_date.replace('-', '')}_001"

    row = {
        "plan_id": plan_id,
        "signal_date": signal_date,
        "suggested_trade_date": suggested_trade_date,
        "ticker": ticker,
        "action": action,
        "suggested_shares": suggested_shares,
        "suggested_price": suggested_price,
        "actual_executable_price": None,
        "actual_executed_price": None,
        "slippage_bp": None,
        "executed": False,
        "not_executed_reason": None,
        "stop_loss_triggered": False,
        "model_reason": model_reason,
        "manual_note": None,
        "linked_actual_trade_id": None,
        "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }

    if os.path.exists(log_path):
        df = pd.read_csv(log_path)
        df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    else:
        df = pd.DataFrame([row])
    df.to_csv(log_path, index=False)


def generate_weekly_report(date, is_complete, missing, plan_df, cfg, output_path):
    """生成每周调仓信号 Markdown 报告。"""
    lines = []
    lines.append("# 每周调仓信号报告")
    lines.append(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"信号日期: {date} (周四收盘)")
    lines.append(f"建议交易日期: {date} 次日开盘")
    lines.append("")

    lines.append("## 1. 数据完整性")
    if is_complete:
        lines.append("- 状态: ✅ 完整")
    else:
        lines.append("- 状态: ❌ 不完整")
        lines.append(f"- 缺失ETF: {', '.join(missing)}")
    lines.append("")

    lines.append("## 2. 调仓信号")
    if not is_complete:
        lines.append("- ⚠️ 数据不完整，不生成交易建议")
        lines.append("- 请补充缺失数据后重新运行")
    elif plan_df.empty:
        lines.append("- 无交易建议")
    else:
        lines.append(f"- 总订单: {len(plan_df)} 笔")
        buy_df = plan_df[plan_df["action"] == "BUY"]
        sell_df = plan_df[plan_df["action"] == "SELL"]
        hold_df = plan_df[plan_df["action"] == "HOLD"]
        lines.append(f"  - BUY: {len(buy_df)} 笔")
        lines.append(f"  - SELL: {len(sell_df)} 笔")
        lines.append(f"  - HOLD: {len(hold_df)} 笔")
        lines.append("")

        lines.append("### 交易明细")
        lines.append("| ticker | action | 当前持仓 | 目标持仓 | 变动 | 建议价 | 建议金额 | 原因 |")
        lines.append("|--------|--------|----------|----------|------|--------|----------|------|")
        for _, row in plan_df.iterrows():
            lines.append(
                f"| {row['ticker']} | {row['action']} | {row['current_shares']} | "
                f"{row['target_shares']} | {row['delta_shares']} | "
                f"{row['estimated_price']:.3f} | {row['estimated_amount']:.2f} | {row['reason']} |"
            )
    lines.append("")

    lines.append("## 3. 纸面交易日志")
    lines.append("- 已追加到 `data/live/paper_trading_log.csv`")
    lines.append("- 请在建议交易日执行后，回填实际成交价格、滑点和未执行原因")
    lines.append("")

    lines.append("## 4. 免责声明")
    lines.append("> 本报告仅供纸面交易验证使用，不构成投资建议。")
    lines.append("> 实际交易决策由用户手动确认。")
    lines.append("> 模型建议基于历史数据，不保证未来表现。")
    lines.append("")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description="Live Weekly Signal")
    parser.add_argument("--date", type=str, default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--output-md", type=str, default=None)
    parser.add_argument("--positions-path", type=str, default=None)
    parser.add_argument("--plan-path", type=str, default=None)
    args = parser.parse_args()

    ensure_dirs()

    cfg = build_config()
    db = ETFDatabase()
    assistant = LiveTradingAssistant(
        positions_path=args.positions_path,
        plan_path=args.plan_path,
        config=cfg,
    )

    print(f"{'='*60}")
    print(f"Live Weekly Signal — {args.date}")
    print(f"{'='*60}")
    print()

    # 1. 数据完整性
    print("检查数据完整性...")
    is_complete, missing = check_data_completeness(db, args.date, FORMAL_POOL)
    if is_complete:
        print(f"OK 数据完整，{len(FORMAL_POOL)} 只ETF")
    else:
        print(f"WARN 数据不完整，缺失 {len(missing)} 只: {', '.join(missing)}")
    print()

    # 2. B0.4 信号
    print("生成 B0.4 信号...")
    target_positions, price_map = get_b0_4_signals(assistant, args.date, cfg)
    if not target_positions:
        print("WARN 无 B0.4 信号")
    else:
        print(f"OK {len(target_positions)} 只目标持仓")
    print()

    # 3. 交易计划
    print("生成交易计划...")
    if not is_complete:
        plan_df = pd.DataFrame()
        print("WARN 数据不完整，跳过交易计划")
    else:
        plan_df = assistant.generate_trade_plan(target_positions, price_map, date=args.date)
        plan_path = args.plan_path or os.path.join(DATA_LIVE_DIR, "latest_trade_plan.csv")
        plan_df.to_csv(plan_path, index=False)
        print(f"OK 交易计划: {plan_path}")
        print(f"  BUY: {len(plan_df[plan_df['action']=='BUY'])} 笔")
        print(f"  SELL: {len(plan_df[plan_df['action']=='SELL'])} 笔")
        print(f"  HOLD: {len(plan_df[plan_df['action']=='HOLD'])} 笔")
    print()

    # 4. 追加纸面日志
    if not plan_df.empty:
        suggested_trade_date = (pd.Timestamp(args.date) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        for _, row in plan_df.iterrows():
            if row["action"] in ("BUY", "SELL"):
                append_paper_log(
                    signal_date=args.date,
                    suggested_trade_date=suggested_trade_date,
                    ticker=row["ticker"],
                    action=row["action"],
                    suggested_shares=abs(row["delta_shares"]),
                    suggested_price=row["estimated_price"],
                    model_reason="调仓" + ("买入" if row["action"] == "BUY" else "卖出"),
                )
        print(f"OK 已追加 {len(plan_df[plan_df['action'].isin(['BUY','SELL'])])} 条记录到 paper_trading_log.csv")
        print()

    # 5. 报告
    output_md = args.output_md or os.path.join(REPORTS_LIVE_DIR, "latest_weekly_signal.md")
    generate_weekly_report(args.date, is_complete, missing, plan_df, cfg, output_md)
    print(f"OK 报告: {output_md}")
    print()

    print(f"{'='*60}")
    print("每周调仓信号完成")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
