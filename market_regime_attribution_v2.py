# -*- coding: utf-8 -*-
"""
market_regime_attribution_v2.py - 市场状态归因分析（修正口径）

修复口径：
1. 每日收益直接按交易日状态归因，不抽取不连续日期后pct_change
2. 回撤按连续状态区间分别计算
3. 买卖配对先在全交易记录完成，再按买入状态、卖出状态分别统计
4. ETF贡献按实际金额PnL计算，不累加pnl_pct
5. 报告区分"持有期间日收益归因"和"交易退出状态归因"

不改策略、不调参数、不改市场状态检测算法。
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


def load_data():
    """加载回测所需数据"""
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
    return market_df, bench_df


def merge_with_regime(df, regime_df, date_col='date'):
    """将DataFrame与市场状态合并（bisect查找最近日期）"""
    df = df.copy()
    df[date_col] = pd.to_datetime(df[date_col])
    regime_df = regime_df.copy().sort_values('date')
    regime_dates = regime_df['date'].tolist()
    regime_ids = regime_df['regime_id'].tolist()
    regime_names = regime_df['regime_name'].tolist()
    regime_confs = regime_df['confidence'].tolist()

    def find_regime(dt):
        idx = bisect.bisect_right(regime_dates, dt) - 1
        if idx >= 0:
            return regime_ids[idx], regime_names[idx], regime_confs[idx]
        return 3, '震荡', 0.5

    regimes = df[date_col].apply(find_regime)
    df['regime_id'] = regimes.apply(lambda x: x[0])
    df['regime_name'] = regimes.apply(lambda x: x[1])
    df['confidence'] = regimes.apply(lambda x: x[2])
    return df


def detect_regime_history(bench_df, market_df):
    """检测历史市场状态序列"""
    detector = MarketRegimeDetector()
    core_tickers = list(config.ETF_UNIVERSE.keys())
    market_for_breadth = market_df[market_df['ticker'].isin(core_tickers)].copy()
    regime_df = detector.detect_history(bench_df, market_for_breadth)
    regime_df['date'] = pd.to_datetime(regime_df['date'])
    return regime_df


def pair_trades(trades_df):
    """
    全交易记录中完成买卖配对。
    返回每笔卖出对应的 (买入日期, 买入状态, 卖出日期, 卖出状态, 金额PnL)。
    """
    if trades_df.empty or 'action' not in trades_df.columns:
        return pd.DataFrame()

    trades = trades_df.copy().sort_values(['date', 'ticker']).reset_index(drop=True)

    buy_queue = defaultdict(list)

    pairs = []
    for _, row in trades.iterrows():
        ticker = row['ticker']
        action = row['action']
        price = row['price']
        shares = row['shares']
        date = row['date']

        if action == 'BUY':
            buy_queue[ticker].append({
                'buy_date': date,
                'cost_price': price,
                'shares': shares,
            })
        elif action in ('SELL', 'STOP_LOSS'):
            if not buy_queue[ticker]:
                continue

            buy_rec = buy_queue[ticker].pop(0)
            pnl_amount = shares * (price - buy_rec['cost_price'])
            commission = row.get('commission', 0)
            pnl_amount -= commission

            pairs.append({
                'ticker': ticker,
                'buy_date': buy_rec['buy_date'],
                'sell_date': date,
                'shares': shares,
                'cost_price': buy_rec['cost_price'],
                'sell_price': price,
                'pnl_amount': pnl_amount,
                'action': action,
                'reason': row.get('reason', ''),
            })

    return pd.DataFrame(pairs)


def calculate_daily_return_attribution(nav_regime_df):
    """
    口径1：每日收益按交易日状态直接归因。
    按状态分组，各连续段落的 (1+日收益) 连乘 - 1。
    """
    nav = nav_regime_df.copy().sort_values('date').reset_index(drop=True)
    nav['daily_return'] = nav['nav'].pct_change()
    nav['segment_id'] = (nav['regime_id'] != nav['regime_id'].shift(1)).cumsum()

    stats = {}
    for rid in [1, 2, 3, 4]:
        name = MarketRegimeDetector.STATE_NAMES[rid]
        mask = nav['regime_id'] == rid
        days = mask.sum()
        if days < 5:
            stats[name] = {'days': 0, 'note': '数据不足'}
            continue

        seg_returns = []
        for seg_id, seg_df in nav[mask].groupby('segment_id'):
            if len(seg_df) < 2:
                continue
            r = (1 + seg_df['daily_return'].dropna()).prod() - 1
            seg_returns.append(r)

        total_contribution = np.prod([1 + r for r in seg_returns]) - 1 if seg_returns else 0

        years = days / 252
        annual = (1 + total_contribution) ** (1 / years) - 1 if years > 0 and total_contribution > -1 else 0

        daily_ret = nav.loc[mask, 'daily_return'].dropna()
        vol = daily_ret.std() * np.sqrt(252) if len(daily_ret) > 1 else 0
        sharpe = annual / vol if vol > 0 else 0

        max_dd = 0
        for seg_id, seg_df in nav[mask].groupby('segment_id'):
            if len(seg_df) < 2:
                continue
            peak = seg_df['nav'].cummax()
            dd = (seg_df['nav'] - peak) / peak
            max_dd = min(max_dd, dd.min())

        avg_positions = nav.loc[mask, 'num_positions'].mean()

        stats[name] = {
            'days': int(days),
            'period_return': total_contribution * 100,
            'annual_return': annual * 100,
            'sharpe': sharpe,
            'max_drawdown': max_dd * 100,
            'avg_positions': avg_positions,
        }
    return stats


def calculate_trade_attribution(pairs_df, nav_regime_df):
    """
    口径3/4/5：交易配对完成后，按买入状态、卖出状态分别统计。
    ETF贡献按实际金额PnL计算。
    """
    if pairs_df.empty:
        return {}, {}

    date_to_regime = {}
    for _, row in nav_regime_df.iterrows():
        date_to_regime[row['date']] = (row['regime_id'], row['regime_name'])

    pairs = pairs_df.copy()
    pairs['buy_date'] = pd.to_datetime(pairs['buy_date'])
    pairs['sell_date'] = pd.to_datetime(pairs['sell_date'])

    pairs['buy_regime_id'] = pairs['buy_date'].map(lambda d: date_to_regime.get(d, (3, '震荡'))[0])
    pairs['buy_regime_name'] = pairs['buy_date'].map(lambda d: date_to_regime.get(d, (3, '震荡'))[1])
    pairs['sell_regime_id'] = pairs['sell_date'].map(lambda d: date_to_regime.get(d, (3, '震荡'))[0])
    pairs['sell_regime_name'] = pairs['sell_date'].map(lambda d: date_to_regime.get(d, (3, '震荡'))[1])

    buy_stats = {}
    sell_stats = {}
    defense_tickers = set(config.DEFENSE_UNIVERSE.keys())
    equity_tickers = set(config.ETF_UNIVERSE.keys())

    for rid in [1, 2, 3, 4]:
        name = MarketRegimeDetector.STATE_NAMES[rid]
        buy_mask = pairs['buy_regime_id'] == rid
        sell_mask = pairs['sell_regime_id'] == rid

        buy_subset = pairs[buy_mask]
        if len(buy_subset) > 0:
            buy_ticker_pnl = buy_subset.groupby('ticker')['pnl_amount'].sum().to_dict()
            buy_equity_pnl = sum(v for k, v in buy_ticker_pnl.items() if k in equity_tickers)
            buy_defense_pnl = sum(v for k, v in buy_ticker_pnl.items() if k in defense_tickers)

            if buy_ticker_pnl:
                best_t = max(buy_ticker_pnl, key=buy_ticker_pnl.get)
                worst_t = min(buy_ticker_pnl, key=buy_ticker_pnl.get)
            else:
                best_t = worst_t = None

            buy_stats[name] = {
                'trades': len(buy_subset),
                'pnl_amount': buy_subset['pnl_amount'].sum(),
                'win_rate': len(buy_subset[buy_subset['pnl_amount'] > 0]) / len(buy_subset) * 100,
                'stop_loss': len(buy_subset[buy_subset['action'] == 'STOP_LOSS']),
                'candidate_exit': len(buy_subset[buy_subset['reason'].str.contains('调出候选', na=False)]),
                'avg_hold': (buy_subset['sell_date'] - buy_subset['buy_date']).dt.days.mean(),
                'median_hold': (buy_subset['sell_date'] - buy_subset['buy_date']).dt.days.median(),
                'equity_pnl': buy_equity_pnl,
                'defense_pnl': buy_defense_pnl,
                'best_ticker': best_t,
                'best_name': config.ETF_UNIVERSE.get(best_t, config.DEFENSE_UNIVERSE.get(best_t, best_t)) if best_t else None,
                'best_pnl': buy_ticker_pnl.get(best_t, 0) if best_t else 0,
                'worst_ticker': worst_t,
                'worst_name': config.ETF_UNIVERSE.get(worst_t, config.DEFENSE_UNIVERSE.get(worst_t, worst_t)) if worst_t else None,
                'worst_pnl': buy_ticker_pnl.get(worst_t, 0) if worst_t else 0,
                'ticker_pnl': buy_ticker_pnl,
            }
        else:
            buy_stats[name] = {'trades': 0}

        sell_subset = pairs[sell_mask]
        if len(sell_subset) > 0:
            sell_ticker_pnl = sell_subset.groupby('ticker')['pnl_amount'].sum().to_dict()
            sell_equity_pnl = sum(v for k, v in sell_ticker_pnl.items() if k in equity_tickers)
            sell_defense_pnl = sum(v for k, v in sell_ticker_pnl.items() if k in defense_tickers)

            if sell_ticker_pnl:
                s_best = max(sell_ticker_pnl, key=sell_ticker_pnl.get)
                s_worst = min(sell_ticker_pnl, key=sell_ticker_pnl.get)
            else:
                s_best = s_worst = None

            sell_stats[name] = {
                'trades': len(sell_subset),
                'pnl_amount': sell_subset['pnl_amount'].sum(),
                'win_rate': len(sell_subset[sell_subset['pnl_amount'] > 0]) / len(sell_subset) * 100,
                'stop_loss': len(sell_subset[sell_subset['action'] == 'STOP_LOSS']),
                'candidate_exit': len(sell_subset[sell_subset['reason'].str.contains('调出候选', na=False)]),
                'avg_hold': (sell_subset['sell_date'] - sell_subset['buy_date']).dt.days.mean(),
                'median_hold': (sell_subset['sell_date'] - sell_subset['buy_date']).dt.days.median(),
                'equity_pnl': sell_equity_pnl,
                'defense_pnl': sell_defense_pnl,
                'best_ticker': s_best,
                'best_name': config.ETF_UNIVERSE.get(s_best, config.DEFENSE_UNIVERSE.get(s_best, s_best)) if s_best else None,
                'best_pnl': sell_ticker_pnl.get(s_best, 0) if s_best else 0,
                'worst_ticker': s_worst,
                'worst_name': config.ETF_UNIVERSE.get(s_worst, config.DEFENSE_UNIVERSE.get(s_worst, s_worst)) if s_worst else None,
                'worst_pnl': sell_ticker_pnl.get(s_worst, 0) if s_worst else 0,
                'ticker_pnl': sell_ticker_pnl,
            }
        else:
            sell_stats[name] = {'trades': 0}

    return buy_stats, sell_stats


def format_report(daily_stats, buy_stats, sell_stats, total_days, total_return, total_sharpe, total_mdd, total_trades, num_pairs, total_pnl):
    """生成修正口径后的归因报告"""
    lines = []

    lines.append("# 市场状态归因分析报告（修正口径 v2）")
    lines.append(f"\n**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**策略**: ETF轮动 v1.2 基线（16只行业ETF）")
    lines.append(f"**数据口径**: 前复权，统一数据至2026-06-05")
    lines.append(f"**市场状态检测**: 基于沪深300趋势/斜率/波动率/市场宽度")
    lines.append(f"**规则**: 连续5日确认切换；不改策略、不调参数、不改市场状态算法")

    lines.append(f"\n## 全区间概览")
    lines.append(f"\n| 指标 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 总交易日 | {total_days} |")
    lines.append(f"| 总收益 | {total_return:.2%} |")
    lines.append(f"| 夏普比率 | {total_sharpe:.2f} |")
    lines.append(f"| 最大回撤 | {total_mdd:.2%} |")
    lines.append(f"| 总交易次数 | {total_trades} |")
    lines.append(f"| 买卖配对数 | {num_pairs} |")
    lines.append(f"| 配对交易金额PnL | {total_pnl:,.2f} |")

    lines.append(f"\n## 第一部分：持有期间日收益归因")
    lines.append(f"\n> 口径：每个交易日的日收益，直接按当日所处状态归属。")
    lines.append(f"> 各状态区间内（1+日收益）连乘，跨段不连续。")

    lines.append(f"\n### 汇总对比")
    lines.append(f"\n| 状态 | 交易日 | 占比 | 日收益贡献 | 年化 | 夏普 | 区间最大回撤 | 平均持仓数 |")
    lines.append(f"|------|--------|------|------------|------|------|--------------|------------|")
    for name in ['强牛', '弱牛', '震荡', '熊市']:
        s = daily_stats.get(name, {})
        if s.get('days', 0) == 0:
            lines.append(f"| {name} | 0 | - | - | - | - | - | - |")
            continue
        pct = s['days'] / total_days * 100 if total_days > 0 else 0
        lines.append(
            f"| {name} | {s['days']} | {pct:.1f}% | "
            f"{s['period_return']:.2f}% | {s['annual_return']:.2f}% | {s['sharpe']:.2f} | "
            f"{s['max_drawdown']:.2f}% | {s['avg_positions']:.1f} |"
        )

    lines.append(f"\n## 第二部分：交易买入状态归因")
    lines.append(f"\n> 口径：每笔买卖配对，按**买入日**所处状态归属。")
    lines.append(f"> ETF贡献按**实际金额PnL**（shares x (sell_price - cost_price) - commission）计算。")

    lines.append(f"\n### 汇总对比")
    lines.append(f"\n| 状态 | 交易对 | 金额PnL | 胜率 | 止损 | 调出候选 | 平均持有 | 中位持有 | 行业ETF PnL | 防御PnL |")
    lines.append(f"|------|--------|---------|------|------|----------|----------|----------|-------------|----------|")
    for name in ['强牛', '弱牛', '震荡', '熊市']:
        s = buy_stats.get(name, {})
        if s.get('trades', 0) == 0:
            lines.append(f"| {name} | 0 | - | - | - | - | - | - | - | - |")
            continue
        lines.append(
            f"| {name} | {s['trades']} | {s['pnl_amount']:,.2f} | {s['win_rate']:.1f}% | "
            f"{s['stop_loss']} | {s['candidate_exit']} | {s['avg_hold']:.1f} | {s['median_hold']:.1f} | "
            f"{s['equity_pnl']:,.2f} | {s['defense_pnl']:,.2f} |"
        )

    for name in ['强牛', '弱牛', '震荡', '熊市']:
        s = buy_stats.get(name, {})
        if s.get('trades', 0) == 0:
            continue
        lines.append(f"\n### {name} 买入归因详细")
        lines.append(f"- 交易对: {s['trades']}")
        lines.append(f"- 金额PnL: {s['pnl_amount']:,.2f}")
        lines.append(f"- 胜率: {s['win_rate']:.1f}%")
        lines.append(f"- 止损次数: {s['stop_loss']}")
        lines.append(f"- 调出候选退出: {s['candidate_exit']}")
        lines.append(f"- 平均持有: {s['avg_hold']:.1f} 天")
        lines.append(f"- 中位持有: {s['median_hold']:.1f} 天")
        lines.append(f"- 行业ETF PnL: {s['equity_pnl']:,.2f}")
        lines.append(f"- 防御资产PnL: {s['defense_pnl']:,.2f}")
        if s['best_ticker']:
            lines.append(f"- 最佳: {s['best_name']} ({s['best_ticker']}) +{s['best_pnl']:,.2f}")
        if s['worst_ticker']:
            lines.append(f"- 最差: {s['worst_name']} ({s['worst_ticker']}) {s['worst_pnl']:,.2f}")

        if s['ticker_pnl']:
            lines.append(f"\n**各ETF金额PnL**:")
            lines.append(f"\n| ETF | 名称 | 金额PnL |")
            lines.append(f"|-----|------|---------|")
            sorted_pnls = sorted(s['ticker_pnl'].items(), key=lambda x: x[1], reverse=True)
            for ticker, pnl in sorted_pnls:
                name_et = config.ETF_UNIVERSE.get(ticker, config.DEFENSE_UNIVERSE.get(ticker, ticker))
                lines.append(f"| {ticker} | {name_et} | {pnl:,.2f} |")

    lines.append(f"\n## 第三部分：交易退出状态归因")
    lines.append(f"\n> 口径：每笔买卖配对，按**卖出日**所处状态归属。")
    lines.append(f"> 反映策略在何种状态下退出持仓。")

    lines.append(f"\n### 汇总对比")
    lines.append(f"\n| 状态 | 退出对 | 金额PnL | 胜率 | 止损 | 调出候选 | 平均持有 | 中位持有 | 行业ETF PnL | 防御PnL |")
    lines.append(f"|------|--------|---------|------|------|----------|----------|----------|-------------|----------|")
    for name in ['强牛', '弱牛', '震荡', '熊市']:
        s = sell_stats.get(name, {})
        if s.get('trades', 0) == 0:
            lines.append(f"| {name} | 0 | - | - | - | - | - | - | - | - |")
            continue
        lines.append(
            f"| {name} | {s['trades']} | {s['pnl_amount']:,.2f} | {s['win_rate']:.1f}% | "
            f"{s['stop_loss']} | {s['candidate_exit']} | {s['avg_hold']:.1f} | {s['median_hold']:.1f} | "
            f"{s['equity_pnl']:,.2f} | {s['defense_pnl']:,.2f} |"
        )

    for name in ['强牛', '弱牛', '震荡', '熊市']:
        s = sell_stats.get(name, {})
        if s.get('trades', 0) == 0:
            continue
        lines.append(f"\n### {name} 退出归因详细")
        lines.append(f"- 退出对: {s['trades']}")
        lines.append(f"- 金额PnL: {s['pnl_amount']:,.2f}")
        lines.append(f"- 胜率: {s['win_rate']:.1f}%")
        lines.append(f"- 止损次数: {s['stop_loss']}")
        lines.append(f"- 调出候选退出: {s['candidate_exit']}")
        lines.append(f"- 平均持有: {s['avg_hold']:.1f} 天")
        lines.append(f"- 中位持有: {s['median_hold']:.1f} 天")
        lines.append(f"- 行业ETF PnL: {s['equity_pnl']:,.2f}")
        lines.append(f"- 防御资产PnL: {s['defense_pnl']:,.2f}")
        if s['best_ticker']:
            lines.append(f"- 最佳: {s['best_name']} ({s['best_ticker']}) +{s['best_pnl']:,.2f}")
        if s['worst_ticker']:
            lines.append(f"- 最差: {s['worst_name']} ({s['worst_ticker']}) {s['worst_pnl']:,.2f}")

        if s['ticker_pnl']:
            lines.append(f"\n**各ETF金额PnL**:")
            lines.append(f"\n| ETF | 名称 | 金额PnL |")
            lines.append(f"|-----|------|---------|")
            sorted_pnls = sorted(s['ticker_pnl'].items(), key=lambda x: x[1], reverse=True)
            for ticker, pnl in sorted_pnls:
                name_et = config.ETF_UNIVERSE.get(ticker, config.DEFENSE_UNIVERSE.get(ticker, ticker))
                lines.append(f"| {ticker} | {name_et} | {pnl:,.2f} |")

    lines.append(f"\n## 关键发现")
    lines.append(f"\n1. **日收益归因**：各状态日收益贡献之和应接近总收益（差值来自状态检测启动前的初期约53天）")
    lines.append(f"2. **买入归因**：策略在何种状态下建立头寸，反映入场决策质量")
    lines.append(f"3. **退出归因**：策略在何种状态下退出头寸，反映退出时机")
    lines.append(f"4. **口径对比**：买入和退出归因的差异，说明同一笔交易在不同阶段的策略表现")

    lines.append(f"\n## 版本边界")
    lines.append(f"\n- v1.2.2 已收口")
    lines.append(f"- v1.3 研究阶段，市场状态归因（修正口径）")
    lines.append(f"- 不改交易规则")
    lines.append(f"- 不设计自适应规则")
    lines.append(f"- 不改市场状态检测算法")

    return '\n'.join(lines)


def main():
    print("="*80)
    print("市场状态归因分析（修正口径 v2）")
    print("="*80)
    print(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    print("[1/5] 加载数据...")
    market_df, bench_df = load_data()
    print(f"  市场数据: {len(market_df)} 行, {market_df['ticker'].nunique()} 只ETF")
    print(f"  基准数据: {len(bench_df)} 行")

    print("\n[2/5] 运行当前基线回测...")
    engine = BacktestEngine()
    result = engine.run(market_df, bench_df)
    if 'error' in result:
        print(f"  回测失败: {result['error']}")
        return

    nav_df = result['nav_df']
    trades_df = result['trades_df']
    print(f"  总收益: {result['total_return']:.2%}")
    print(f"  最大回撤: {result['max_drawdown']:.2%}")
    print(f"  夏普: {result['sharpe_ratio']:.2f}")
    print(f"  交易次数: {result['num_trades']}")

    print("\n[3/5] 检测市场状态历史...")
    regime_df = detect_regime_history(bench_df, market_df)
    print(f"  状态检测完成: {len(regime_df)} 个交易日")
    state_dist = regime_df['regime_name'].value_counts()
    for name, count in state_dist.items():
        print(f"    {name}: {count} 天 ({count/len(regime_df)*100:.1f}%)")

    print("\n[4/5] 合并数据并完成交易配对...")
    nav_regime = merge_with_regime(nav_df, regime_df)
    pairs_df = pair_trades(trades_df)
    num_pairs = len(pairs_df)
    total_pnl = pairs_df['pnl_amount'].sum() if not pairs_df.empty else 0
    print(f"  买卖配对: {num_pairs} 对")
    print(f"  配对金额PnL: {total_pnl:,.2f}")

    print("\n[5/5] 计算三种归因口径...")
    daily_stats = calculate_daily_return_attribution(nav_regime)
    buy_stats, sell_stats = calculate_trade_attribution(pairs_df, nav_regime)

    print("\n生成报告...")
    report = format_report(
        daily_stats, buy_stats, sell_stats,
        total_days=len(nav_df),
        total_return=result['total_return'],
        total_sharpe=result['sharpe_ratio'],
        total_mdd=result['max_drawdown'],
        total_trades=result['num_trades'],
        num_pairs=num_pairs,
        total_pnl=total_pnl,
    )

    report_path = 'reports/market_regime_attribution_v2.md'
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)
    print(f"\n报告已保存: {report_path}")

    print("\n" + "="*80)
    print("摘要")
    print("="*80)

    print("\n--- 日收益归因 ---")
    for name in ['强牛', '弱牛', '震荡', '熊市']:
        s = daily_stats.get(name, {})
        if s.get('days', 0) > 0:
            print(f"{name}: {s['days']}天 ({s['days']/len(nav_df)*100:.1f}%) 日收益贡献={s['period_return']:.2f}% 回撤={s['max_drawdown']:.2f}%")

    print("\n--- 买入归因 ---")
    for name in ['强牛', '弱牛', '震荡', '熊市']:
        s = buy_stats.get(name, {})
        if s.get('trades', 0) > 0:
            print(f"{name}: {s['trades']}对 PnL={s['pnl_amount']:,.2f} 胜率={s['win_rate']:.1f}% 持有={s['avg_hold']:.1f}天")

    print("\n--- 退出归因 ---")
    for name in ['强牛', '弱牛', '震荡', '熊市']:
        s = sell_stats.get(name, {})
        if s.get('trades', 0) > 0:
            print(f"{name}: {s['trades']}对 PnL={s['pnl_amount']:,.2f} 胜率={s['win_rate']:.1f}% 持有={s['avg_hold']:.1f}天")

    print(f"\n[OK] 市场状态归因分析（修正口径）完成")


if __name__ == '__main__':
    main()
