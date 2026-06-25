#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日止损检查脚本

用法：
    py scripts/live_check_stop_loss.py --date 2026-06-26

功能：
    1. 读取真实持仓
    2. 检查是否触发止损
    3. 生成 Markdown 报告
"""

import argparse, os, sys
sys.path.insert(0, "src")

from datetime import datetime
from live_trading_assistant import LiveTradingAssistant


def main():
    parser = argparse.ArgumentParser(description="每日止损检查")
    parser.add_argument("--date", type=str, default=datetime.now().strftime("%Y-%m-%d"),
                        help="检查日期 (YYYY-MM-DD)")
    parser.add_argument("--stop-loss", type=float, default=0.08,
                        help="止损比例 (默认 8%)")
    parser.add_argument("--output", type=str, default=None,
                        help="报告输出路径")
    parser.add_argument("--positions-path", type=str, default=None)
    args = parser.parse_args()

    assistant = LiveTradingAssistant(positions_path=args.positions_path)

    # 更新配置中的止损比例
    assistant.config["stop_loss"] = args.stop_loss

    # 检查止损
    alerts = assistant.check_stop_loss()
    if alerts.empty:
        print(f"✅ {args.date} 无触发止损的持仓。")
    else:
        print(f"⚠️ {args.date} 触发 {len(alerts)} 只持仓止损：")
        for _, r in alerts.iterrows():
            print(f"  {r['ticker']}: 成本={r['cost_price']:.3f} 现价={r['current_price']:.3f} 亏损={r['loss_pct']:.2%}")

    # 生成报告
    content = assistant.generate_daily_alert(date=args.date, output_path=args.output)
    print(f"\n报告已保存至: {args.output or assistant.generate_daily_alert.__defaults__[0]}")


if __name__ == "__main__":
    main()
