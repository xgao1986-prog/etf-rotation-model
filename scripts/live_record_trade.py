#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Trade record script

Usage:
    py scripts/live_record_trade.py \
        --date 2026-06-26 \
        --ticker 512400.SH \
        --action BUY \
        --shares 100 \
        --price 0.650 \
        --commission 0.2 \
        --note "First build"

Features:
    1. Record trade to trades CSV
    2. Update actual positions
"""

import argparse, os, sys
sys.path.insert(0, "src")

from datetime import datetime
from live_trading_assistant import LiveTradingAssistant, ActualTrade


def main():
    parser = argparse.ArgumentParser(description="Record actual trade")
    parser.add_argument("--date", type=str, required=True, help="Trade date (YYYY-MM-DD)")
    parser.add_argument("--ticker", type=str, required=True, help="ETF ticker")
    parser.add_argument("--action", type=str, required=True, choices=["BUY", "SELL"], help="Action")
    parser.add_argument("--shares", type=int, required=True, help="Shares")
    parser.add_argument("--price", type=float, required=True, help="Trade price")
    parser.add_argument("--commission", type=float, default=0.1, help="Commission")
    parser.add_argument("--note", type=str, default="", help="Note")
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

    assistant.apply_trade(trade)
    print("OK Recorded: %s %s %s %d shares @ %.3f" % (args.date, args.action, args.ticker, args.shares, args.price))
    print("   Commission: %.2f Note: %s" % (args.commission, args.note))

    df = assistant.load_positions()
    cash_row = df[df["ticker"] == "__CASH__"]
    if not cash_row.empty:
        cash = float(cash_row.iloc[0]["market_value"])
        print("   Cash: %,.2f" % cash)

    ticker_row = df[df["ticker"] == args.ticker]
    if not ticker_row.empty:
        shares = int(ticker_row.iloc[0]["shares"])
        print("   Position: %s %d shares" % (args.ticker, shares))


if __name__ == "__main__":
    main()
