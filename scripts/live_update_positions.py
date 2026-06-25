#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Position price update script

Usage:
    py scripts/live_update_positions.py --date 2026-06-26

Features:
    1. Read actual positions CSV
    2. Get latest prices from database
    3. Update market values
    4. Save back to CSV
"""

import argparse, os, sys
sys.path.insert(0, "src")

from datetime import datetime
from live_trading_assistant import LiveTradingAssistant
from database import ETFDatabase


def main():
    parser = argparse.ArgumentParser(description="Position price update")
    parser.add_argument("--date", type=str, default=datetime.now().strftime("%Y-%m-%d"),
                        help="Update date (YYYY-MM-DD)")
    parser.add_argument("--positions-path", type=str, default=None,
                        help="Positions CSV path")
    parser.add_argument("--trades-path", type=str, default=None)
    parser.add_argument("--plan-path", type=str, default=None)
    args = parser.parse_args()

    assistant = LiveTradingAssistant(
        positions_path=args.positions_path,
        trades_path=args.trades_path,
        plan_path=args.plan_path,
    )

    df = assistant.load_positions()
    if df.empty:
        print("Positions empty, nothing to update.")
        return

    tickers = [t for t in df["ticker"].unique() if t != "__CASH__"]
    if not tickers:
        print("No holdings to update.")
        return

    db = ETFDatabase()
    price_map = {}
    for t in tickers:
        try:
            data = db.get_market_data(ticker=t, start_date=args.date, end_date=args.date)
            if not data.empty:
                price_map[t] = data["close"].iloc[-1]
            else:
                print("  WARN No close price for %s" % t)
        except Exception as e:
            print("  WARN Failed to get price for %s: %s" % (t, e))

    if not price_map:
        print("No prices fetched, skipping update.")
        return

    assistant.update_prices(price_map, date=args.date)
    print("\nOK Updated %d positions, date: %s" % (len(price_map), args.date))
    for t, p in price_map.items():
        print("  %s: %.3f" % (t, p))


if __name__ == "__main__":
    main()
