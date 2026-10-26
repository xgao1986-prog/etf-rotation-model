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
from typing import Dict, Tuple, Optional
from live_trading_assistant import LiveTradingAssistant
from config import ETF_UNIVERSE, DEFENSE_UNIVERSE, build_config
from database import ETFDatabase, apply_adjustment_for_scoring
from rebalance_planner import plan_rebalance_v2_5
import pandas as pd


SHARE_UNIT = 100


def get_b0_4_signals(assistant, date, cfg, db=None):
    """
    从数据库读取不晚于 date 的最新评分，使用 plan_rebalance_v2_5 生成目标持仓。

    参数:
        assistant: LiveTradingAssistant 实例
        date: 日期字符串（YYYY-MM-DD），只使用不晚于此日期的评分和行情
        cfg: 策略配置
        db: 可选的 ETFDatabase 实例（测试中注入 mock）

    返回:
        (target_positions, price_map)
        target_positions: {ticker: int_shares}，只包含 ETF 代码和正整数股数
        price_map: {ticker: float_price}
    """
    if db is None:
        db = ETFDatabase()

    # 读取评分
    scores = db.get_scores()
    if scores.empty:
        print("WARN 数据库无评分数据，无法生成目标持仓")
        return {}, {}

    # 只使用不晚于指定日期的评分
    scores_dates = pd.to_datetime(scores['date'])
    target_dt = pd.to_datetime(date)
    eligible = scores[scores_dates <= target_dt]
    if eligible.empty:
        print(f"WARN {date} 及之前无评分数据")
        return {}, {}

    latest_date = eligible['date'].max()
    latest = eligible[eligible['date'] == latest_date].copy()

    # 筛选候选（账户设置一致性复审补修 ①②，2026-10-22）：
    # 走共享模块 entry_filter.select_candidates，与正式 B0.4 生产信号路径
    # （generate_signals + _rebalance_v2 二次过滤）逐条对齐：弱市55分门槛、
    # 防御资格宽松档、prev_close>ma20、ma20_slope、数据成熟期 history_count>=51。
    # 评分字段缺失/无效时抛出 ValueError，停止生成交易计划（不再有宽松回退）。
    from paper_trading.entry_filter import select_candidates

    # 富化 prev_close / history_count：daily_scores 不含这两列，从行情表计算。
    # prev_close = 评分日前一交易日收盘；history_count = 截至评分日的历史行数。
    latest = latest.copy()
    latest_ts = pd.to_datetime(latest_date)
    prev_close_map = {}
    history_count_map = {}
    for t in latest['ticker'].unique():
        try:
            hist = db.get_market_data(ticker=t, end_date=latest_ts.strftime('%Y-%m-%d'))
        except Exception as exc:
            raise ValueError(f"评分字段富化失败：{t} 行情查询出错（{exc}），已停止生成交易计划。")
        if hist is None or hist.empty:
            raise ValueError(
                f"评分字段富化失败：{t} 无行情数据，无法核实 prev_close 与数据成熟期"
                f"（history_count>=51），已停止生成交易计划（不猜测、不回退）。")
        hist = hist.sort_values('date')
        hist = hist[pd.to_datetime(hist['date']) <= latest_ts]
        if hist.empty:
            raise ValueError(
                f"评分字段富化失败：{t} 在评分日 {latest_ts.strftime('%Y-%m-%d')} 及之前"
                f"无行情数据，无法核实 prev_close 与数据成熟期，已停止生成交易计划。")
        hist_dates = pd.to_datetime(hist['date'])
        # 与正式评分引擎一致，入场判断使用评分调整价；price_map 仍读取真实收盘价。
        hist = apply_adjustment_for_scoring(hist)
        before = hist[hist_dates < latest_ts]
        if before.empty:
            raise ValueError(
                f"评分字段富化失败：{t} 在评分日 {latest_ts.strftime('%Y-%m-%d')} 之前"
                f"无行情数据，无法计算 prev_close，已停止生成交易计划。")
        prev_close_map[t] = float(before['close'].iloc[-1])
        history_count_map[t] = int(len(hist))
    latest['prev_close'] = latest['ticker'].map(prev_close_map)
    latest['history_count'] = latest['ticker'].map(history_count_map)

    min_score = cfg.get('min_total_score', 40)
    industry_candidates, defense_candidates, meta = select_candidates(
        latest,
        industry_tickers=set(ETF_UNIVERSE.keys()),
        defense_tickers=set(DEFENSE_UNIVERSE.keys()),
        min_total_score=min_score,
        min_trend_score=cfg.get('min_trend_score', 15),
        min_confirm_score=cfg.get('min_confirm_score', 4),
        context="实盘调仓脚本",
    )

    print(f"OK 评分日期: {latest_date} (请求日期上限: {date})")
    if meta['market_quality_poor']:
        print(f"OK 弱市判定成立（核心池动量中位数<0），行业总分门槛升至 {meta['effective_min_total']:.0f} 分")
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

    # 从 final_state['positions'] 提取目标持仓
    # final_state 结构: {'cash': float, 'positions': {ticker: int}, ...}
    target_positions = {}
    positions = final_state.get('positions', {})
    for t, shares in positions.items():
        if t != '__CASH__' and isinstance(shares, int) and shares > 0:
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

    try:
        target_positions, price_map_signals = get_b0_4_signals(assistant, args.date, cfg)
    except ValueError as exc:
        # 评分字段缺失/无效或富化失败：明确提示并以非零码退出，不生成任何计划
        print(f"ERROR {exc}")
        sys.exit(1)
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
