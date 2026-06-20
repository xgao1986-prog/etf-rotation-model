# -*- coding: utf-8 -*-
"""
diagnose_weak_bull_execution.py - 诊断弱牛交易执行贡献-12.86万

目标：按调仓日逐笔拆分，找出-12.86万的来源

分析维度：
1. 按调仓日分组：买入当日损益、卖出当日机会损益、佣金
2. 按ETF统计：哪些ETF贡献主要损失
3. 按年份统计：哪些年份损失大
4. 损失来源诊断：买入追高 vs 卖出过早 vs 异常日期
5. 单笔异常：损失最大的10笔调仓日交易

不改策略、不调参数。
"""
import sys
sys.path.insert(0, 'src')

import pandas as pd
import numpy as np
import bisect
from collections import defaultdict
from datetime import datetime
from database import ETFDatabase
from backtest import BacktestEngine
from market_regime import MarketRegimeDetector
import config


COMMON_CUTOFF = pd.Timestamp('2026-06-05')


def load_data():
    db = ETFDatabase()
    tickers = list(config.ETF_UNIVERSE.keys()) + list(config.DEFENSE_UNIVERSE.keys())
    market_dfs = []
    for ticker in tickers:
        df = db.get_market_data(ticker=ticker)
        if not df.empty:
            market_dfs.append(df)
    market_df = pd.concat(market_dfs, ignore_index=True) if market_dfs else pd.DataFrame()
    market_df['date'] = pd.to_datetime(market_df['date'])
    bench_df = db.get_market_data(ticker=config.BENCHMARK)
    bench_df['date'] = pd.to_datetime(bench_df['date'])
    market_df = market_df[market_df['date'] <= COMMON_CUTOFF].copy()
    bench_df = bench_df[bench_df['date'] <= COMMON_CUTOFF].copy()
    return market_df, bench_df


def detect_regime_history(bench_df, market_df):
    detector = MarketRegimeDetector()
    core_tickers = list(config.ETF_UNIVERSE.keys())
    market_for_breadth = market_df[market_df['ticker'].isin(core_tickers)].copy()
    regime_df = detector.detect_history(bench_df, market_for_breadth)
    regime_df['date'] = pd.to_datetime(regime_df['date'])
    return regime_df


def build_effective_price_map(market_df):
    prices = defaultdict(dict)
    last_valid_close = {}
    last_valid_open = {}
    all_dates = sorted(market_df['date'].unique())
    for date in all_dates:
        day_data = market_df[market_df['date'] == date]
        for _, row in day_data.iterrows():
            ticker = row['ticker']
            open_p = row['open']
            close_p = row['close']
            if pd.notna(close_p) and close_p > 0:
                last_valid_close[ticker] = close_p
            if pd.notna(open_p) and open_p > 0:
                last_valid_open[ticker] = open_p
            prices[ticker][date] = {
                'open': open_p if pd.notna(open_p) and open_p > 0 else last_valid_open.get(ticker, 0),
                'close': close_p if pd.notna(close_p) and close_p > 0 else last_valid_close.get(ticker, 0),
            }
    return prices


def diagnose_weak_bull_execution(nav_df, trades_df, price_map, regime_df, industry_tickers, defense_tickers):
    """
    核心诊断：弱牛交易执行-12.86万的来源
    
    逐笔交易分析：
    - 买入：买入价 vs 当日收盘价（买入后当日损益）
    - 卖出：卖出价 vs 当日收盘价（卖出当日机会损益）
    - 佣金：每笔交易成本
    
    叠加市场状态：只关注交易日为弱牛的日子
    """
    
    # 1. 只提取弱牛交易日的交易
    regime_df = regime_df.sort_values('date').reset_index(drop=True)
    regime_dates = regime_df['date'].tolist()
    
    def get_regime(dt):
        idx = bisect.bisect_right(regime_dates, dt) - 1
        if idx >= 0:
            return regime_df.iloc[idx]['regime_id'], regime_df.iloc[idx]['regime_name']
        return 3, '震荡'
    
    # 逐笔交易标注状态和计算损益
    trade_analysis = []
    
    for _, trade in trades_df.iterrows():
        date = pd.to_datetime(trade['date'])
        regime_id, regime_name = get_regime(date)
        
        # 只分析弱牛 + 行业ETF的交易
        if regime_name != '弱牛' or trade['ticker'] not in industry_tickers:
            continue
        
        ticker = trade['ticker']
        action = trade['action']
        shares = trade['shares']
        price = trade['price']
        commission = trade.get('commission', 0)
        pnl_pct = trade.get('pnl_pct', 0)
        
        p_today = price_map.get(ticker, {}).get(date)
        if p_today is None:
            continue
        
        open_price = p_today['open']
        close_price = p_today['close']
        
        if action == 'BUY':
            # 买入后当日损益 = 收盘价 - 买入价
            # 如果买入价是开盘价（调仓日买入），当日损益 = close - open
            buy_pnl = shares * (close_price - price)
            # 是否追高：买入后当天跌了（close < open）
            is_chase = bool(close_price < price)
            trade_analysis.append({
                'date': date, 'ticker': ticker, 'action': 'BUY',
                'shares': shares, 'price': price, 'open': open_price, 'close': close_price,
                'commission': commission, 'pnl_pct': pnl_pct,
                'execution_pnl': buy_pnl, 'is_chase': is_chase,
                'reason': trade.get('reason', ''),
            })
        elif action in ('SELL', 'STOP_LOSS'):
            # 卖出当日机会损益：如果当天开盘卖出，少赚了/多亏了多少
            # sell_price = open_price (调仓日卖出)，则卖出当日损益 = shares * (open - close)
            sell_pnl = shares * (price - close_price)
            # 是否过早卖出：卖出后当天涨了（close > open）
            is_early = bool(close_price > price)
            trade_analysis.append({
                'date': date, 'ticker': ticker, 'action': 'SELL',
                'shares': shares, 'price': price, 'open': open_price, 'close': close_price,
                'commission': commission, 'pnl_pct': pnl_pct,
                'execution_pnl': sell_pnl, 'is_early': is_early,
                'reason': trade.get('reason', ''),
            })
    
    analysis_df = pd.DataFrame(trade_analysis)
    if analysis_df.empty:
        return None, None, None, None
    
    analysis_df['year'] = analysis_df['date'].dt.year
    
    # 2. 汇总统计
    
    # 2.1 按动作汇总
    by_action = analysis_df.groupby('action').agg(
        count=('execution_pnl', 'count'),
        total_pnl=('execution_pnl', 'sum'),
        total_commission=('commission', 'sum'),
        avg_pnl=('execution_pnl', 'mean'),
        median_pnl=('execution_pnl', 'median'),
    )
    
    # 2.2 按ETF汇总
    by_etf = analysis_df.groupby('ticker').agg(
        buy_count=('execution_pnl', lambda x: len(x[analysis_df.loc[x.index, 'action'] == 'BUY'])),
        sell_count=('execution_pnl', lambda x: len(x[analysis_df.loc[x.index, 'action'] == 'SELL'])),
        total_pnl=('execution_pnl', 'sum'),
        total_commission=('commission', 'sum'),
        avg_pnl=('execution_pnl', 'mean'),
    ).sort_values('total_pnl')
    
    # 2.3 按年份汇总
    by_year = analysis_df.groupby('year').agg(
        count=('execution_pnl', 'count'),
        total_pnl=('execution_pnl', 'sum'),
        total_commission=('commission', 'sum'),
        avg_pnl=('execution_pnl', 'mean'),
    )
    
    # 2.4 按调仓日汇总（所有弱牛日的交易合计）
    by_date = analysis_df.groupby('date').agg(
        buy_count=('execution_pnl', lambda x: len(x[analysis_df.loc[x.index, 'action'] == 'BUY'])),
        sell_count=('execution_pnl', lambda x: len(x[analysis_df.loc[x.index, 'action'] == 'SELL'])),
        total_pnl=('execution_pnl', 'sum'),
        total_commission=('commission', 'sum'),
    ).sort_values('total_pnl')
    
    # 2.5 买入追高统计
    buy_df = analysis_df[analysis_df['action'] == 'BUY']
    chase_stats = {
        'total_buys': len(buy_df),
        'chase_count': buy_df['is_chase'].sum(),
        'chase_rate': buy_df['is_chase'].sum() / len(buy_df) * 100 if len(buy_df) > 0 else 0,
        'chase_pnl': buy_df[buy_df['is_chase'] == True]['execution_pnl'].sum(),
        'good_buy_pnl': buy_df[buy_df['is_chase'] == False]['execution_pnl'].sum(),
    }
    
    # 2.6 卖出过早统计
    sell_df = analysis_df[analysis_df['action'].isin(['SELL', 'STOP_LOSS'])]
    early_stats = {
        'total_sells': len(sell_df),
        'early_count': sell_df['is_early'].sum(),
        'early_rate': sell_df['is_early'].sum() / len(sell_df) * 100 if len(sell_df) > 0 else 0,
        'early_pnl': sell_df[sell_df['is_early'] == True]['execution_pnl'].sum(),
        'good_sell_pnl': sell_df[sell_df['is_early'] == False]['execution_pnl'].sum(),
    }
    
    # 2.7 损失最大的10笔单笔交易
    worst_trades = analysis_df.nsmallest(20, 'execution_pnl')[['date', 'ticker', 'action', 'shares', 'price', 'open', 'close', 'execution_pnl', 'commission', 'reason']]
    
    # 2.8 损失最大的10个调仓日
    worst_dates = by_date.head(10)
    
    return by_action, by_etf, by_year, by_date, chase_stats, early_stats, worst_trades, worst_dates, analysis_df


def format_diagnosis_report(by_action, by_etf, by_year, by_date, chase_stats, early_stats, worst_trades, worst_dates, analysis_df, total_weak_bull_exec):
    lines = []
    
    lines.append("# 弱牛交易执行贡献诊断报告")
    lines.append(f"\n**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**诊断目标**: 弱牛交易执行贡献 = {total_weak_bull_exec:,.2f} 元")
    lines.append(f"**数据口径**: 只分析弱牛交易日的行业ETF交易，前复权，统一至2026-06-05")
    lines.append(f"**买入价**: 调仓日开盘价（策略实际成交价）")
    lines.append(f"**卖出价**: 调仓日开盘价（策略实际成交价）")
    lines.append(f"**买入后当日损益**: shares × (收盘价 - 买入价)")
    lines.append(f"**卖出当日机会损益**: shares × (卖出价 - 收盘价)")
    lines.append(f"**追高定义**: 买入后当天收盘价 < 买入价（当天即跌）")
    lines.append(f"**过早定义**: 卖出后当天收盘价 > 卖出价（卖出后当天即涨）")
    
    # 1. 总体拆分
    lines.append(f"\n## 1. 总体拆分（弱牛交易执行 = 买入当日损益 + 卖出当日机会损益）")
    
    total_buy = by_action.loc['BUY', 'total_pnl'] if 'BUY' in by_action.index else 0
    total_sell = by_action.loc['SELL', 'total_pnl'] if 'SELL' in by_action.index else 0
    total_commission = by_action['total_commission'].sum() if not by_action.empty else 0
    
    lines.append(f"\n| 项目 | 笔数 | 总金额 | 平均单笔 | 中位数 | 佣金 |")
    lines.append(f"|------|------|--------|----------|--------|------|")
    for action in by_action.index:
        row = by_action.loc[action]
        lines.append(f"| {action} | {row['count']:.0f} | {row['total_pnl']:,.2f} | {row['avg_pnl']:,.2f} | {row['median_pnl']:,.2f} | {row['total_commission']:,.2f} |")
    
    lines.append(f"\n- 弱牛买入当日损益合计: {total_buy:,.2f}")
    lines.append(f"- 弱牛卖出当日机会损益合计: {total_sell:,.2f}")
    lines.append(f"- 弱牛交易佣金合计: {total_commission:,.2f}")
    lines.append(f"- 交易执行净贡献（不含佣金）: {total_buy + total_sell:,.2f}")
    lines.append(f"- 交易执行总贡献（含佣金）: {total_buy + total_sell - total_commission:,.2f}")
    
    # 2. 买入追高诊断
    lines.append(f"\n## 2. 买入追高诊断")
    lines.append(f"- 弱牛总买入笔数: {chase_stats['total_buys']}")
    lines.append(f"- 追高笔数（买入后当天跌）: {chase_stats['chase_count']}")
    lines.append(f"- 追高率: {chase_stats['chase_rate']:.1f}%")
    lines.append(f"- 追高买入当日损益: {chase_stats['chase_pnl']:,.2f}")
    lines.append(f"- 非追高买入当日损益: {chase_stats['good_buy_pnl']:,.2f}")
    
    # 3. 卖出过早诊断
    lines.append(f"\n## 3. 卖出过早诊断")
    lines.append(f"- 弱牛总卖出笔数: {early_stats['total_sells']}")
    lines.append(f"- 过早笔数（卖出后当天涨）: {early_stats['early_count']}")
    lines.append(f"- 过早率: {early_stats['early_rate']:.1f}%")
    lines.append(f"- 过早卖出当日机会损益: {early_stats['early_pnl']:,.2f}")
    lines.append(f"- 非过早卖出当日机会损益: {early_stats['good_sell_pnl']:,.2f}")
    
    # 4. 按ETF统计
    lines.append(f"\n## 4. 按ETF统计（弱牛交易执行贡献）")
    lines.append(f"\n| ETF | 买入笔数 | 卖出笔数 | 当日损益合计 | 佣金合计 | 平均单笔 | 名称 |")
    lines.append(f"|-----|----------|----------|--------------|----------|----------|------|")
    
    for ticker in by_etf.index:
        row = by_etf.loc[ticker]
        name = config.ETF_UNIVERSE.get(ticker, ticker)
        lines.append(f"| {ticker} | {row['buy_count']:.0f} | {row['sell_count']:.0f} | {row['total_pnl']:,.2f} | {row['total_commission']:,.2f} | {row['avg_pnl']:,.2f} | {name} |")
    
    # 5. 按年份统计
    lines.append(f"\n## 5. 按年份统计（弱牛交易执行贡献）")
    lines.append(f"\n| 年份 | 交易笔数 | 当日损益合计 | 佣金合计 | 平均单笔 |")
    lines.append(f"|------|----------|--------------|----------|----------|")
    
    for year in by_year.index:
        row = by_year.loc[year]
        lines.append(f"| {year} | {row['count']:.0f} | {row['total_pnl']:,.2f} | {row['total_commission']:,.2f} | {row['avg_pnl']:,.2f} |")
    
    # 6. 损失最大的单笔交易
    lines.append(f"\n## 6. 损失最大的20笔单笔交易（弱牛）")
    lines.append(f"\n| 日期 | ETF | 动作 | 股数 | 成交价 | 开盘 | 收盘 | 当日损益 | 佣金 | 原因 |")
    lines.append(f"|------|-----|------|------|--------|------|------|----------|------|------|")
    
    for _, row in worst_trades.iterrows():
        lines.append(f"| {row['date'].strftime('%Y-%m-%d')} | {row['ticker']} | {row['action']} | {row['shares']:.0f} | {row['price']:.3f} | {row['open']:.3f} | {row['close']:.3f} | {row['execution_pnl']:,.2f} | {row['commission']:,.2f} | {row['reason']} |")
    
    # 7. 损失最大的调仓日
    lines.append(f"\n## 7. 损失最大的10个调仓日（弱牛）")
    lines.append(f"\n| 日期 | 买入笔数 | 卖出笔数 | 当日损益合计 | 佣金合计 | 净损失 |")
    lines.append(f"|------|----------|----------|--------------|----------|--------|")
    
    for date, row in worst_dates.iterrows():
        net_loss = row['total_pnl'] - row['total_commission']
        lines.append(f"| {date.strftime('%Y-%m-%d')} | {row['buy_count']:.0f} | {row['sell_count']:.0f} | {row['total_pnl']:,.2f} | {row['total_commission']:,.2f} | {net_loss:,.2f} |")
    
    # 8. 关键发现
    lines.append(f"\n## 8. 关键发现")
    
    # 计算各来源占比
    total_exec = total_buy + total_sell
    buy_ratio = total_buy / total_exec * 100 if total_exec != 0 else 0
    sell_ratio = total_sell / total_exec * 100 if total_exec != 0 else 0
    
    lines.append(f"\n1. **买入 vs 卖出占比**: 买入贡献 {buy_ratio:.1f}%, 卖出贡献 {sell_ratio:.1f}%")
    lines.append(f"2. **追高率**: {chase_stats['chase_rate']:.1f}% 的买入是追高（当天即跌）")
    lines.append(f"3. **过早率**: {early_stats['early_rate']:.1f}% 的卖出是过早（卖出后当天即涨）")
    lines.append(f"4. **损失最大ETF**: {by_etf.index[0] if not by_etf.empty else 'N/A'} ({by_etf.iloc[0]['total_pnl']:,.2f})")
    lines.append(f"5. **损失最大年份**: {by_year['total_pnl'].idxmin() if not by_year.empty else 'N/A'} ({by_year['total_pnl'].min():,.2f})")
    
    # 异常日期分析
    worst_date_pct = worst_dates['total_pnl'].sum() / total_exec * 100 if total_exec != 0 else 0
    lines.append(f"6. **异常日期集中度**: 前10个最差调仓日占总损失的 {worst_date_pct:.1f}%")
    
    lines.append(f"\n## 9. 诊断结论与下一步建议")
    
    if chase_stats['chase_rate'] > 50:
        lines.append(f"- **主要问题：买入追高**。超过一半的新买入在当天即亏损，说明调仓日买入时机有问题。")
        lines.append(f"  建议方向：测试延迟成交（次日开盘、VWAP）、或引入调仓日开盘前的信号确认机制。")
    
    if early_stats['early_rate'] > 50:
        lines.append(f"- **次要问题：卖出过早**。超过一半的卖出在当天即错失上涨，说明卖出时机过于敏感。")
        lines.append(f"  建议方向：测试卖出延迟（次日收盘确认）、或引入卖出评分缓冲。")
    
    if worst_date_pct > 50:
        lines.append(f"- **异常日期集中**：少数调仓日贡献了大部分损失，建议检查这些日期的市场环境（如利空消息、流动性冲击）。")
    
    lines.append(f"\n## 版本边界")
    lines.append(f"- B0 已冻结（Git: c54c1f6）")
    lines.append(f"- 本报告为诊断阶段，不改策略")
    lines.append(f"- 下一步建议：根据诊断结果，单变量测试调仓日/成交时点/退出规则")
    
    return '\n'.join(lines)


def main():
    print("="*80)
    print("弱牛交易执行贡献诊断")
    print("="*80)
    print(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    print("[1/4] 加载数据...")
    market_df, bench_df = load_data()
    print(f"  市场数据: {len(market_df)} 行, {market_df['ticker'].nunique()} 只ETF")
    print(f"  基准数据: {len(bench_df)} 行")

    print("\n[2/4] 运行B0回测...")
    engine = BacktestEngine()
    result = engine.run(market_df, bench_df)
    if 'error' in result:
        print(f"  回测失败: {result['error']}")
        return
    nav_df = result['nav_df']
    trades_df = result['trades_df']
    print(f"  总收益: {result['total_return']:.2%}, 夏普: {result['sharpe_ratio']:.2f}")
    
    print("\n[3/4] 检测市场状态...")
    regime_df = detect_regime_history(bench_df, market_df)
    print(f"  状态检测完成: {len(regime_df)} 个交易日")
    
    print("\n[4/4] 诊断弱牛交易执行...")
    price_map = build_effective_price_map(market_df)
    
    industry_tickers = set(config.ETF_UNIVERSE.keys())
    defense_tickers = set(config.DEFENSE_UNIVERSE.keys())
    
    by_action, by_etf, by_year, by_date, chase_stats, early_stats, worst_trades, worst_dates, analysis_df = diagnose_weak_bull_execution(
        nav_df, trades_df, price_map, regime_df, industry_tickers, defense_tickers
    )
    
    if by_action is None:
        print("  弱牛无交易记录")
        return
    
    # 计算总弱牛交易执行贡献（从归因中）
    # 重新运行一次归因获取弱牛交易执行
    from market_regime_attribution_v5b import calculate_true_daily_attribution, aggregate_by_state, build_trade_map
    trades_map = build_trade_map(trades_df)
    daily_df, _, _ = calculate_true_daily_attribution(
        nav_df, trades_map, price_map, regime_df, industry_tickers, defense_tickers
    )
    state_stats, _ = aggregate_by_state(daily_df, industry_tickers, defense_tickers)
    total_weak_bull_exec = state_stats['弱牛']['execution_contrib']
    
    print(f"\n  弱牛交易执行贡献: {total_weak_bull_exec:,.2f}")
    print(f"  买入笔数: {chase_stats['total_buys']}, 卖出笔数: {early_stats['total_sells']}")
    print(f"  追高率: {chase_stats['chase_rate']:.1f}%, 过早率: {early_stats['early_rate']:.1f}%")
    
    print("\n生成报告...")
    report = format_diagnosis_report(
        by_action, by_etf, by_year, by_date, chase_stats, early_stats,
        worst_trades, worst_dates, analysis_df, total_weak_bull_exec
    )
    
    report_path = 'reports/weak_bull_execution_diagnosis.md'
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)
    print(f"\n报告已保存: {report_path}")
    
    print(f"\n{'='*80}")
    print(f"[OK] 弱牛交易执行诊断完成")
    print(f"{'='*80}")


if __name__ == '__main__':
    main()
