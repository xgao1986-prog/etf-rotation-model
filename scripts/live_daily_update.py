#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Live Daily Update — v0.2

每日运行：
1. 更新 B0.4 正式池数据（16+2+沪深300）
2. 运行数据准入检查（完整性、缺失）
3. 基于真实持仓检查止损
4. 输出每日检查报告
5. 数据不完整时给出警告，不生成交易建议

用法：
    py scripts/live_daily_update.py --date 2026-06-26

输出：
    data/live/paper_trading_log.csv
    reports/live/latest_daily_check.md
"""

import argparse, os, sys, warnings
from datetime import datetime, timedelta
import pandas as pd

sys.path.insert(0, "src")

from config import build_config, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK
from database import ETFDatabase
from live_trading_assistant import LiveTradingAssistant


# 正式池 = 16只行业ETF + 2只防御ETF + 沪深300基准
FORMAL_POOL = list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys()) + [BENCHMARK]

DATA_LIVE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "live")
REPORTS_LIVE_DIR = os.path.join(os.path.dirname(__file__), "..", "reports", "live")


def ensure_dirs():
    os.makedirs(DATA_LIVE_DIR, exist_ok=True)
    os.makedirs(REPORTS_LIVE_DIR, exist_ok=True)


def check_data_completeness(db, date, tickers):
    """检查数据完整性。

    返回：
        (is_complete, missing_tickers, last_dates)
    """
    # 获取最近5个交易日的数据
    end_date = datetime.strptime(date, "%Y-%m-%d")
    start_date = end_date - timedelta(days=30)
    start_str = start_date.strftime("%Y-%m-%d")

    market_df = db.get_market_data(ticker=tickers, start_date=start_str, end_date=date)

    missing = []
    last_dates = {}

    for ticker in tickers:
        t_data = market_df[market_df["ticker"] == ticker]
        if t_data.empty:
            missing.append(ticker)
            last_dates[ticker] = None
        else:
            last_date = t_data["date"].max()
            last_dates[ticker] = last_date
            if last_date < pd.Timestamp(date):
                # 检查是否差太多
                gap = (pd.Timestamp(date) - last_date).days
                if gap > 7:
                    missing.append(ticker)

    return len(missing) == 0, missing, last_dates


def run_daily_stop_loss_check(assistant, cfg, date):
    """基于真实持仓检查止损。

    返回：
        (alerts_df, report_lines)
    """
    lines = []
    df = assistant.load_positions()
    if df.empty:
        lines.append("持仓为空，跳过止损检查")
        return pd.DataFrame(), lines

    stop_loss_pct = cfg.get("stop_loss", 0.08)
    alerts = assistant.check_stop_loss(df, stop_loss_pct=stop_loss_pct)

    if alerts.empty:
        lines.append("未触发止损")
    else:
        lines.append(f"触发止损 {len(alerts)} 只：")
        for _, row in alerts.iterrows():
            lines.append(
                f"  - {row['ticker']}: 成本价 {row['cost_price']:.3f} "
                f"→ 当前价 {row['current_price']:.3f} "
                f"(跌幅 {row['loss_pct']:.2%})"
            )
    return alerts, lines


def append_paper_log(date, ticker, action, suggested_shares, suggested_price,
                     model_reason, stop_loss_triggered=False):
    """追加纸面交易日志记录。

    如果文件不存在，创建表头。
    """
    log_path = os.path.join(DATA_LIVE_DIR, "paper_trading_log.csv")
    plan_id = f"{date.replace('-', '')}_E01"

    row = {
        "plan_id": plan_id,
        "signal_date": date,
        "suggested_trade_date": date,  # 止损建议当日或次日
        "ticker": ticker,
        "action": action,
        "suggested_shares": suggested_shares,
        "suggested_price": suggested_price,
        "actual_executable_price": None,
        "actual_executed_price": None,
        "slippage_bp": None,
        "executed": False,
        "not_executed_reason": None,
        "stop_loss_triggered": stop_loss_triggered,
        "model_reason": model_reason,
        "manual_note": None,
        "linked_actual_trade_id": None,
        "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }

    if os.path.exists(log_path):
        df = pd.read_csv(log_path)
        df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    else:
        df = pd.DataFrame([row])
    df.to_csv(log_path, index=False)


def generate_daily_report(date, is_complete, missing, last_dates, alerts, cfg, output_path):
    """生成每日检查 Markdown 报告。"""
    lines = []
    lines.append("# 每日检查报告")
    lines.append(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"检查日期: {date}")
    lines.append("")

    lines.append("## 1. 数据完整性")
    if is_complete:
        lines.append(f"- 状态: ✅ 完整")
        lines.append(f"- 正式池 {len(FORMAL_POOL)} 只ETF数据均可用")
    else:
        lines.append(f"- 状态: ❌ 不完整")
        lines.append(f"- 缺失或滞后的ETF: {', '.join(missing)}")
    lines.append("")

    lines.append("## 2. 最后数据日期")
    lines.append("| ticker | 最后数据日期 |")
    lines.append("|--------|--------------|")
    for ticker in FORMAL_POOL:
        ld = last_dates.get(ticker)
        ld_str = ld.strftime("%Y-%m-%d") if ld is not None else "N/A"
        lines.append(f"| {ticker} | {ld_str} |")
    lines.append("")

    lines.append("## 3. 止损检查")
    if alerts.empty:
        lines.append("- 未触发止损")
    else:
        lines.append(f"- 触发止损 {len(alerts)} 只：")
        for _, row in alerts.iterrows():
            lines.append(
                f"  - {row['ticker']}: 成本价 {row['cost_price']:.3f} "
                f"→ 当前价 {row['current_price']:.3f} "
                f"(跌幅 {row['loss_pct']:.2%})"
            )
    lines.append("")

    lines.append("## 4. 交易建议")
    if not is_complete:
        lines.append("- ⚠️ 数据不完整，不生成正式交易建议")
        lines.append("- 请补充缺失数据后重新运行")
    elif not alerts.empty:
        lines.append("- 止损触发，请手动确认是否执行")
        for _, row in alerts.iterrows():
            lines.append(
                f"  - 建议 SELL {row['ticker']}: "
                f"持仓 {row['shares']} 股，建议次日开盘卖出"
            )
    else:
        lines.append("- 无异常，无需操作")
    lines.append("")

    lines.append("## 5. 免责声明")
    lines.append("> 本报告仅供纸面交易验证使用，不构成投资建议。")
    lines.append("> 实际交易决策由用户手动确认。")
    lines.append("")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description="Live Daily Update")
    parser.add_argument("--date", type=str, default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--output-md", type=str, default=None)
    parser.add_argument("--positions-path", type=str, default=None)
    parser.add_argument("--config", type=str, default=None)
    args = parser.parse_args()

    ensure_dirs()

    cfg = build_config()
    if args.config:
        import json
        cfg.update(json.loads(args.config))

    db = ETFDatabase()
    assistant = LiveTradingAssistant(positions_path=args.positions_path, config=cfg)

    print(f"{'='*60}")
    print(f"Live Daily Update — {args.date}")
    print(f"{'='*60}")
    print()

    # 1. 数据完整性检查
    print("检查数据完整性...")
    is_complete, missing, last_dates = check_data_completeness(db, args.date, FORMAL_POOL)
    if is_complete:
        print(f"OK 数据完整，{len(FORMAL_POOL)} 只ETF")
    else:
        print(f"WARN 数据不完整，缺失 {len(missing)} 只: {', '.join(missing)}")
    print()

    # 2. 止损检查
    print("检查止损...")
    alerts, stop_lines = run_daily_stop_loss_check(assistant, cfg, args.date)
    for line in stop_lines:
        print(line)
    print()

    # 3. 如果止损触发，追加纸面日志
    if not alerts.empty:
        for _, row in alerts.iterrows():
            append_paper_log(
                args.date, row["ticker"], "SELL", row["shares"], row["current_price"],
                model_reason="止损", stop_loss_triggered=True
            )
        print(f"OK 已追加 {len(alerts)} 条止损记录到 paper_trading_log.csv")
        print()

    # 4. 生成报告
    output_md = args.output_md or os.path.join(REPORTS_LIVE_DIR, "latest_daily_check.md")
    generate_daily_report(args.date, is_complete, missing, last_dates, alerts, cfg, output_md)
    print(f"OK 报告: {output_md}")
    print()

    print(f"{'='*60}")
    print("每日检查完成")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
