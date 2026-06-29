#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Weekly rebalance plan script

Usage:
    py scripts/live_generate_trade_plan.py --date 2026-06-26

Features:
    1. Run B0.4 signals to get target positions (using plan_rebalance_v2_5)
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
from rebalance_planner import plan_rebalance_v2_5
import pandas as pd


SHARE_UNIT = 100


def get_b0_4_signals(assistant, date, cfg):
    """
    从数据库 daily_scores 读取最新评分，使用 plan_rebalance_v2_5 生成目标持仓。

    参数:
        assistant: LiveTradingAssistant 实例
        date: 日期字符串（用于价格查询）
        cfg: 策略配置

    返回:
        (target_positions, price_map)
    """
    db = ETFDatabase()

    # 读取最新评分
    scores = db.get_scores()
    if scores.empty:
        print("WARN 数据库无评分数据，无法生成目标持仓")
        return {}, {}

    # 取最新日期
    latest_date = scores['date'].max()
    latest = scores[scores['date'] == latest_date].copy()

    # 筛选行业ETF候选（来自 ETF_UNIVERSE）
    industry_scores = latest[latest['ticker'].isin(ETF_UNIVERSE.keys())]
    min_score = cfg.get('min_total_score', 40)
    qualified_industry = industry_scores[industry_scores['total_score'] >= min_score].sort_values('total_score', ascending=False)
    industry_candidates = [(row['ticker'], float(row['total_score'])) for _, row in qualified_industry.iterrows()]

    # 防御资产候选（来自 DEFENSE_UNIVERSE）
    defense_scores = latest[latest['ticker'].isin(DEFENSE_UNIVERSE.keys())]
    defense_candidates = [(row['ticker'], float(row['total_score'])) for _, row in defense_scores.iterrows()]

    print(f"OK 最新评分日期: {latest_date}")
    print(f"OK 行业候选: {len(industry_candidates)} 只")
    print(f"OK 防御候选: {len(defense_candidates)} 只")

    # 读取当前持仓
    positions_df = assistant.load_positions()
    current_positions = {}
    cash = 0.0
    for _, r in positions_df.iterrows():
        if r['ticker'] == '__CASH__':
            cash = float(r['market_value'])
        elif r['ticker'] != '__CASH__':
            current_positions[r['ticker']] = int(r['shares'])

    # 计算 NAV
    nav = cash
    for t, s in current_positions.items():
        price = float(positions_df[positions_df['ticker'] == t]['current_price'].iloc[0]) if not positions_df[positions_df['ticker'] == t].empty else 0
        nav += s * price

    # 获取所有需要的价格（行业 + 防御 + 当前持仓）
    all_tickers = set(
        [t for t, _ in industry_candidates] +
        [t for t, _ in defense_candidates] +
        list(current_positions.keys())
    )
    price_map = {}
    date_str = latest_date.strftime('%Y-%m-%d') if hasattr(latest_date, 'strftime') else str(latest_date)
    for t in all_tickers:
        try:
            data = db.get_market_data(ticker=t, start_date=date_str, end_date=date_str)
            if not data.empty:
                price_map[t] = data['close'].iloc[-1]
        except Exception:
            pass

    # 从当前持仓补充价格
    for _, r in positions_df.iterrows():
        if r['ticker'] != '__CASH__' and r['ticker'] not in price_map and r['current_price'] > 0:
            price_map[r['ticker']] = r['current_price']

    # 使用 plan_rebalance_v2_5 生成调仓计划
    max_holdings = cfg.get('max_holdings', 5)
    max_position = cfg.get('max_position_per_etf', 0.20)

    trades, final_state = plan_rebalance_v2_5(
        nav=nav,
        cash=cash,
        current_positions=current_positions,
        industry_candidates=industry_candidates,
        defense_candidates=defense_candidates,
        prices=price_map,
        industry_tickers=set(ETF_UNIVERSE.keys()),
        defense_tickers=set(DEFENSE_UNIVERSE.keys()),
        max_industry_holdings=max_holdings,
        max_defense_holdings=2,
        max_total_holdings=max_holdings,
        max_position_per_etf=max_position,
        max_total_position=1.0,
        commission_rate=0.0003,
        min_commission=5.0,
        lot_size=100,
        defense_enabled=True,
    )

    # 从 final_state 提取目标持仓
    target_positions = {}
    for t, shares in final_state.items():
        if t != '__CASH__' and shares > 0:
            target_positions[t] = shares

    print(f"OK 总资产: {nav:,.2f}, 现金: {cash:,.2f}")
    print(f"OK 目标持仓: {target_positions}")

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
