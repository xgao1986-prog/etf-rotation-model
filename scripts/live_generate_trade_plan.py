#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每周调仓计划生成脚本

用法：
    py scripts/live_generate_trade_plan.py --date 2026-06-26

功能：
    1. 运行 B0.4 信号，获取目标持仓
    2. 读取真实持仓
    3. 对比生成交易计划
    4. 保存 CSV 和 Markdown 报告
"""

import argparse, os, sys
sys.path.insert(0, "src")

from datetime import datetime
from live_trading_assistant import LiveTradingAssistant
from config import ETF_UNIVERSE, DEFENSE_UNIVERSE, build_config
from strategy import StrategyEngine
from database import ETFDatabase
import pandas as pd


def get_b0_4_signals(date: str, cfg: dict):
    """运行 B0.4 信号，返回目标持仓 {ticker: target_shares}。"""
    db = ETFDatabase()
    tickers = list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())

    # 获取最新评分
    market_df = db.get_market_data(ticker=tickers)
    if market_df.empty:
        return {}

    engine = StrategyEngine(cfg)
    signals = engine.generate_signals(market_df)
    if signals.empty:
        return {}

    latest = signals[signals["date"] == signals["date"].max()].copy()
    latest = latest[latest["qualified"]].sort_values("total_score", ascending=False)

    # 选取前 max_holdings 只
    max_holdings = cfg.get("max_holdings", 5)
    selected = latest.head(max_holdings)

    # 计算目标股数（简化：每只等权，总金额 = 总资产 × 单只上限）
    # 这里需要真实持仓中的总资产，所以返回 selected ticker 列表即可
    return {row["ticker"]: 0 for _, row in selected.iterrows()}  # 0 表示目标持仓由用户决定


def main():
    parser = argparse.ArgumentParser(description="每周调仓计划生成")
    parser.add_argument("--date", type=str, default=datetime.now().strftime("%Y-%m-%d"),
                        help="日期 (YYYY-MM-DD)")
    parser.add_argument("--output-csv", type=str, default=None,
                        help="交易计划 CSV 输出路径")
    parser.add_argument("--output-md", type=str, default=None,
                        help="报告 Markdown 输出路径")
    parser.add_argument("--positions-path", type=str, default=None)
    parser.add_argument("--trades-path", type=str, default=None)
    parser.add_argument("--plan-path", type=str, default=None)
    args = parser.parse_args()

    cfg, _ = build_config()
    assistant = LiveTradingAssistant(
        positions_path=args.positions_path,
        trades_path=args.trades_path,
        plan_path=args.plan_path,
        config=cfg,
    )

    # 获取 B0.4 目标持仓
    target_positions = get_b0_4_signals(args.date, cfg)
    if not target_positions:
        print("⚠️ 今日无 B0.4 信号，生成空计划。")
        target_positions = {}

    # 读取真实持仓，获取当前价格
    positions_df = assistant.load_positions()
    price_map = {}
    for _, r in positions_df.iterrows():
        if r["ticker"] != "__CASH__" and r["current_price"] > 0:
            price_map[r["ticker"]] = r["current_price"]

    # 简化：目标持仓股数 = 当前持仓股数（HOLD），后续可扩展为基于仓位的计算
    # v0.1 中，调仓计划主要是对比真实持仓和 B0.4 推荐信号
    # 如果真实持仓中有不在 B0.4 推荐中的，标记为 SELL
    # 如果 B0.4 推荐中有不在真实持仓中的，标记为 BUY
    actual_tickers = set(positions_df[positions_df["ticker"] != "__CASH__"]["ticker"].unique())
    target_tickers = set(target_positions.keys())

    # 构建目标持仓（简化版：v0.1 只给出 BUY/SELL 建议，不计算精确股数）
    # 实际使用中，用户需要手动输入目标持仓股数
    # 这里生成一个计划模板
    all_tickers = actual_tickers | target_tickers
    plan_positions = {}
    for t in all_tickers:
        if t in actual_tickers and t in target_tickers:
            plan_positions[t] = positions_df[positions_df["ticker"] == t]["shares"].iloc[0]
        elif t in actual_tickers and t not in target_tickers:
            plan_positions[t] = 0  # 建议卖出
        else:
            plan_positions[t] = 0  # 建议买入，股数由用户决定

    # 生成交易计划
    plan_df = assistant.generate_trade_plan(plan_positions, price_map, date=args.date)

    # 保存到指定路径
    if args.output_csv:
        plan_df.to_csv(args.output_csv, index=False)
        print(f"✅ 交易计划 CSV 已保存: {args.output_csv}")

    # 生成 Markdown 报告
    md = assistant.generate_weekly_plan(plan_df, date=args.date, output_path=args.output_md)
    print(f"✅ 调仓报告已保存: {args.output_md or assistant.generate_weekly_plan.__defaults__[0]}")

    print(f"\n📋 计划摘要:")
    print(f"  买入: {len(plan_df[plan_df['action']=='BUY'])} 笔")
    print(f"  卖出: {len(plan_df[plan_df['action']=='SELL'])} 笔")
    print(f"  保留: {len(plan_df[plan_df['action']=='HOLD'])} 笔")


if __name__ == "__main__":
    main()
