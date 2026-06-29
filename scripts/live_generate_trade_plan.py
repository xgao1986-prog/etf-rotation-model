#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Weekly rebalance plan script

Usage:
    py scripts/live_generate_trade_plan.py --date 2026-06-26

Features:
    1. Run B0.4 signals to get target positions
    2. Read actual positions
    3. Generate trade plan by comparison
    4. Save CSV and Markdown report
"""

import argparse, os, sys
sys.path.insert(0, "src")

from datetime import datetime
from live_trading_assistant import LiveTradingAssistant
from config import ETF_UNIVERSE, DEFENSE_UNIVERSE, build_config
from strategy import StrategyEngine
from database import ETFDatabase
import pandas as pd


SHARE_UNIT = 100


def get_b0_4_signals(assistant, date, cfg):
    """
    从数据库 daily_scores 读取最新评分，生成目标持仓。

    参数:
        assistant: LiveTradingAssistant 实例
        date: 日期字符串（用于价格查询）
        cfg: 策略配置

    返回:
        (target_positions, price_map)
    """
    db = ETFDatabase()

    # 读取最新评分
    # 读取最新评分，只筛选 ETF_UNIVERSE（行业ETF）中的 ticker
    scores = db.get_scores()
    if scores.empty:
        print("WARN 数据库无评分数据，无法生成目标持仓")
        return {}, {}

    # 只保留行业ETF（排除宽基补仓和防御资产）
    scores = scores[scores['ticker'].isin(ETF_UNIVERSE.keys())]
    if scores.empty:
        print("WARN 无行业ETF评分数据")
        return {}, {}

    # 取最新日期
    latest_date = scores['date'].max()
    latest = scores[scores['date'] == latest_date].copy()

    # 筛选 qualified（total_score >= min_total_score）
    min_score = cfg.get('min_total_score', 40)
    qualified = latest[latest['total_score'] >= min_score].sort_values('total_score', ascending=False)

    if qualified.empty:
        print("WARN 最新日期无 qualified 评分，无法生成目标持仓")
        return {}, {}

    max_holdings = cfg.get('max_holdings', 5)
    selected = qualified.head(max_holdings)

    print(f"OK 最新评分日期: {latest_date}")
    print(f"OK 选中 {len(selected)} 只: {list(selected['ticker'].values)}")

    # 计算总资产
    positions_df = assistant.load_positions()
    cash_rows = positions_df[positions_df['ticker'] == '__CASH__']
    total_asset = float(cash_rows.iloc[0]['market_value']) if not cash_rows.empty else 0.0
    for _, r in positions_df.iterrows():
        if r['ticker'] != '__CASH__' and r['current_price'] > 0:
            total_asset += r['market_value']

    max_position = cfg.get('max_position_per_etf', 0.20)
    target_amount = total_asset * max_position
    print(f"OK 总资产: {total_asset:,.2f}, 单只目标金额: {target_amount:,.2f}")

    # 获取最新价格
    tickers = selected['ticker'].unique().tolist()
    price_map = {}
    # 将 Timestamp 转为字符串日期，避免 SQLite 字符串比较问题
    date_str = latest_date.strftime('%Y-%m-%d') if hasattr(latest_date, 'strftime') else str(latest_date)
    for t in tickers:
        try:
            data = db.get_market_data(ticker=t, start_date=date_str, end_date=date_str)
            if not data.empty:
                price_map[t] = data['close'].iloc[-1]
        except Exception:
            pass

    target_positions = {}
    for _, row in selected.iterrows():
        t = row['ticker']
        price = price_map.get(t, 0)
        if price > 0 and target_amount > 0:
            shares = int(target_amount / price // SHARE_UNIT * SHARE_UNIT)
            target_positions[t] = shares
        else:
            target_positions[t] = 0
            print(f"WARN {t}: 价格缺失或目标金额为零，目标股数设为 0")

    return target_positions, price_map


def main():
    parser = argparse.ArgumentParser(description="Weekly rebalance plan")
    parser.add_argument("--date", type=str, default=datetime.now().strftime("%Y-%m-%d"),
                        help="Date (YYYY-MM-DD)")
    parser.add_argument("--output-csv", type=str, default=None,
                        help="Trade plan CSV output path")
    parser.add_argument("--output-md", type=str, default=None,
                        help="Report Markdown output path")
    parser.add_argument("--positions-path", type=str, default=None)
    parser.add_argument("--trades-path", type=str, default=None)
    parser.add_argument("--plan-path", type=str, default=None)
    args = parser.parse_args()

    cfg = build_config()
    assistant = LiveTradingAssistant(
        positions_path=args.positions_path,
        trades_path=args.trades_path,
        plan_path=args.plan_path,
        config=cfg,
    )

    target_positions, price_map_signals = get_b0_4_signals(assistant, args.date, cfg)
    if not target_positions:
        print("WARN No B0.4 signals today, generating empty plan.")
        target_positions = {}

    positions_df = assistant.load_positions()

    price_map = {}
    for _, r in positions_df.iterrows():
        if r["ticker"] != "__CASH__" and r["current_price"] > 0:
            price_map[r["ticker"]] = r["current_price"]

    # 合并价格：优先使用信号查询到的价格（更接近实时），持仓价格为备选
    for t, p in price_map_signals.items():
        if t not in price_map or price_map[t] <= 0:
            price_map[t] = p

    actual_tickers = set(positions_df[positions_df["ticker"] != "__CASH__"]["ticker"].unique())
    target_tickers = set(target_positions.keys())

    all_tickers = actual_tickers | target_tickers
    plan_positions = {}
    for t in all_tickers:
        if t in target_tickers:
            plan_positions[t] = target_positions[t]
        elif t in actual_tickers:
            plan_positions[t] = 0

    plan_df = assistant.generate_trade_plan(plan_positions, price_map, date=args.date)

    if not actual_tickers:
        print("INFO 持仓为空（只有现金），生成首次建仓建议。")

    if args.output_csv:
        plan_df.to_csv(args.output_csv, index=False)
        print("OK Trade plan CSV saved: %s" % args.output_csv)

    default_md = os.path.join(os.path.dirname(assistant.plan_path), "..", "reports", "live",
                                 "weekly_rebalance_plan_%s.md" % args.date)
    output_md = args.output_md or default_md
    md = assistant.generate_weekly_plan(plan_df, date=args.date, output_path=output_md)
    print("OK Report saved: %s" % output_md)

    print("\nPlan summary:")
    print("  BUY: %d orders" % len(plan_df[plan_df['action']=='BUY']))
    print("  SELL: %d orders" % len(plan_df[plan_df['action']=='SELL']))
    print("  HOLD: %d orders" % len(plan_df[plan_df['action']=='HOLD']))


if __name__ == "__main__":
    main()
