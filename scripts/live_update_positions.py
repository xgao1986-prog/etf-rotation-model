#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实盘持仓价格更新脚本

用法：
    py scripts/live_update_positions.py --date 2026-06-26

功能：
    1. 读取真实持仓 CSV
    2. 从数据库获取最新价格
    3. 更新持仓市值
    4. 保存回 CSV
"""

import argparse, os, sys
sys.path.insert(0, "src")

from datetime import datetime
from live_trading_assistant import LiveTradingAssistant
from database import ETFDatabase


def main():
    parser = argparse.ArgumentParser(description="实盘持仓价格更新")
    parser.add_argument("--date", type=str, default=datetime.now().strftime("%Y-%m-%d"),
                        help="更新日期 (YYYY-MM-DD)")
    parser.add_argument("--positions-path", type=str, default=None,
                        help="持仓 CSV 路径")
    parser.add_argument("--trades-path", type=str, default=None,
                        help="成交记录 CSV 路径")
    parser.add_argument("--plan-path", type=str, default=None,
                        help="交易计划 CSV 路径")
    args = parser.parse_args()

    assistant = LiveTradingAssistant(
        positions_path=args.positions_path,
        trades_path=args.trades_path,
        plan_path=args.plan_path,
    )

    # 读取持仓
    df = assistant.load_positions()
    if df.empty:
        print("持仓为空，无需更新。")
        return

    # 获取需要更新的 ticker
    tickers = [t for t in df["ticker"].unique() if t != "__CASH__"]
    if not tickers:
        print("无持仓需要更新。")
        return

    # 从数据库获取价格
    db = ETFDatabase()
    price_map = {}
    for t in tickers:
        try:
            data = db.get_market_data(ticker=t, start_date=args.date, end_date=args.date)
            if not data.empty:
                price_map[t] = data["close"].iloc[-1]
            else:
                print(f"  ⚠️ 未获取到 {t} 的收盘价")
        except Exception as e:
            print(f"  ⚠️ 获取 {t} 价格失败: {e}")

    if not price_map:
        print("未获取到任何价格，跳过更新。")
        return

    # 更新价格
    assistant.update_prices(price_map, date=args.date)
    print(f"\n✅ 已更新 {len(price_map)} 只持仓价格，日期: {args.date}")
    for t, p in price_map.items():
        print(f"  {t}: {p:.3f}")


if __name__ == "__main__":
    main()
