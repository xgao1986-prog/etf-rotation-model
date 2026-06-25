#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实际成交记录脚本

用法：
    py scripts/live_record_trade.py \
        --date 2026-06-26 \
        --ticker 512400.SH \
        --action BUY \
        --shares 100 \
        --price 0.650 \
        --commission 0.2 \
        --note "首次建仓"

功能：
    1. 记录实际成交到 trades CSV
    2. 更新真实持仓
"""

import argparse, os, sys
sys.path.insert(0, "src")

from datetime import datetime
from live_trading_assistant import LiveTradingAssistant, ActualTrade


def main():
    parser = argparse.ArgumentParser(description="记录实际成交")
    parser.add_argument("--date", type=str, required=True, help="成交日期 (YYYY-MM-DD)")
    parser.add_argument("--ticker", type=str, required=True, help="ETF 代码")
    parser.add_argument("--action", type=str, required=True, choices=["BUY", "SELL"], help="操作")
    parser.add_argument("--shares", type=int, required=True, help="成交股数")
    parser.add_argument("--price", type=float, required=True, help="成交价格")
    parser.add_argument("--commission", type=float, default=0.1, help="佣金")
    parser.add_argument("--note", type=str, default="", help="备注")
    parser.add_argument("--positions-path", type=str, default=None)
    parser.add_argument("--trades-path", type=str, default=None)
    args = parser.parse_args()

    assistant = LiveTradingAssistant(
        positions_path=args.positions_path,
        trades_path=args.trades_path,
    )

    trade = ActualTrade(
        date=args.date,
        ticker=args.ticker,
        action=args.action,
        shares=args.shares,
        actual_price=args.price,
        commission=args.commission,
        note=args.note,
    )

    # 应用成交到持仓
    assistant.apply_trade(trade)
    print(f"✅ 已记录成交: {args.date} {args.action} {args.ticker} {args.shares}股 @ {args.price}")
    print(f"   佣金: {args.commission} 备注: {args.note}")

    # 显示当前持仓
    df = assistant.load_positions()
    cash_row = df[df["ticker"] == "__CASH__"]
    if not cash_row.empty:
        cash = float(cash_row.iloc[0]["market_value"])
        print(f"   当前现金: {cash:,.2f}")

    ticker_row = df[df["ticker"] == args.ticker]
    if not ticker_row.empty:
        shares = int(ticker_row.iloc[0]["shares"])
        print(f"   当前持仓: {args.ticker} {shares}股")


if __name__ == "__main__":
    main()
