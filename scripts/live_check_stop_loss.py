#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Daily stop loss check script

Usage:
    py scripts/live_check_stop_loss.py --date 2026-06-26

Features:
    1. Read actual positions
    2. Check stop loss triggers
    3. Generate Markdown report
"""

import argparse, os, sys
sys.path.insert(0, "src")

from datetime import datetime
from live_trading_assistant import LiveTradingAssistant


def main():
    parser = argparse.ArgumentParser(description="Daily stop loss check")
    parser.add_argument("--date", type=str, default=datetime.now().strftime("%Y-%m-%d"),
                        help="Check date (YYYY-MM-DD)")
    parser.add_argument("--stop-loss", type=float, default=0.08,
                        help="Stop loss ratio (default 8%%)")
    parser.add_argument("--output", type=str, default=None,
                        help="Report output path")
    parser.add_argument("--positions-path", type=str, default=None)
    args = parser.parse_args()

    assistant = LiveTradingAssistant(positions_path=args.positions_path)
    assistant.config["stop_loss"] = args.stop_loss

    alerts = assistant.check_stop_loss()
    if alerts.empty:
        print("OK %s No stop loss triggered." % args.date)
    else:
        print("WARN %s %d positions triggered stop loss:" % (args.date, len(alerts)))
        for _, r in alerts.iterrows():
            print("  %s: cost=%.3f current=%.3f loss=%.2f%%" % (r['ticker'], r['cost_price'], r['current_price'], r['loss_pct']*100))

    default_md = os.path.join(os.path.dirname(assistant.positions_path), "..", "reports", "live",
                                 "daily_stop_loss_alert_%s.md" % args.date)
    output_path = args.output or default_md
    content = assistant.generate_daily_alert(date=args.date, output_path=output_path)
    print("\nReport saved to: %s" % output_path)


if __name__ == "__main__":
    main()
