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
    Run B0.4 signals, return target positions {ticker: target_shares}.

    Logic:
    - Read total asset from actual positions
    - Each recommended ETF target amount = total_asset * max_position_per_etf (default 20%)
    - Target shares = target_amount / current_price, floor to 100 multiples
    """
    db = ETFDatabase()
    tickers = list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())

    market_df = db.get_market_data(ticker=tickers)
    if market_df.empty:
        return {}

    engine = StrategyEngine(cfg)
    signals = engine.generate_signals(market_df)
    if signals.empty:
        return {}

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
            shares = int(target_amount / price // SHARE_UNIT * SHARE_UNIT)
            target_positions[t] = shares
        else:
            target_positions[t] = 0

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

    cfg, _ = build_config()
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
