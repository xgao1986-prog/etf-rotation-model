# -*- coding: utf-8 -*-
"""
market_regime_attribution_v3b.py - 持仓期间浮盈浮亏归因（修正平均口径）

核心口径：每笔持仓期间，按持仓期间的市场状态分布归因日收益。
关键修正：使用平均日收益（而非日收益和），避免同时持仓的重复计算问题。

目标：
1. 判断持仓期间是否跟上了该状态的大盘动量
2. 持仓标的是否是涨势较好的标的
3. 计算持仓胜率（PnL>0）
4. 计算日收益胜率（日收益>0）和跑赢基准胜率（日收益>基准日收益）

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
    detector = MarketRegimeDetector()
    core_tickers = list(config.ETF_UNIVERSE.keys())
    market_for_breadth = market_df[market_df['ticker'].isin(core_tickers)].copy()
    regime_df = detector.detect_history(bench_df, market_for_breadth)
    regime_df['date'] = pd.to_datetime(regime_df['date'])
    return regime_df


def pair_trades(trades_df):
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
            buy_queue[ticker].append({'buy_date': date, 'cost_price': price, 'shares': shares})
        elif action in ('SELL', 'STOP_LOSS'):
            if not buy_queue[ticker]:
                continue
            buy_rec = buy_queue[ticker].pop(0)
            pnl_amount = shares * (price - buy_rec['cost_price'])
            commission = row.get('commission', 0)
            pnl_amount -= commission
            pairs.append({
                'ticker': ticker, 'buy_date': buy_rec['buy_date'], 'sell_date': date,
                'shares': shares, 'cost_price': buy_rec['cost_price'], 'sell_price': price,
                'pnl_amount': pnl_amount, 'action': action, 'reason': row.get('reason', ''),
            })
    return pd.DataFrame(pairs)


def calculate_position_period_attribution(pairs_df, nav_regime_df, bench_df):
    """
    核心：每笔持仓期间，按持仓期间的市场状态分布归因日收益。
    修正：使用平均日收益，避免同时持仓的重复计算。
    """
    if pairs_df.empty or nav_regime_df.empty:
        return {}, pd.DataFrame()

    nav = nav_regime_df.copy().sort_values('date').reset_index(drop=True)
    nav['daily_return'] = nav['nav'].pct_change()

    bench = bench_df.copy().sort_values('date').reset_index(drop=True)
    bench['bench_return'] = bench['close'].pct_change()
    date_to_bench = {}
    for _, row in bench.iterrows():
        date_to_bench[row['date']] = row['bench_return']

    date_to_regime = {}
    for _, row in nav.iterrows():
        date_to_regime[row['date']] = (row['regime_id'], row['regime_name'])

    position_stats = []
    # 收集所有日收益（按状态）
    all_daily_returns = {1: [], 2: [], 3: [], 4: []}  # 策略日收益
    all_bench_returns = {1: [], 2: [], 3: [], 4: []}  # 基准日收益

    for _, pos in pairs_df.iterrows():
        buy_date = pos['buy_date']
        sell_date = pos['sell_date']
        ticker = pos['ticker']
        pnl_amount = pos['pnl_amount']
        shares = pos['shares']
        cost = shares * pos['cost_price']

        period = nav[(nav['date'] > buy_date) & (nav['date'] <= sell_date)].copy()
        if period.empty:
            continue

        start_nav = nav[nav['date'] == buy_date]['nav'].iloc[0] if not nav[nav['date'] == buy_date].empty else None
        end_nav = nav[nav['date'] == sell_date]['nav'].iloc[0] if not nav[nav['date'] == sell_date].empty else None
        if start_nav is None or end_nav is None or start_nav <= 0:
            continue

        period_total_return = (end_nav / start_nav) - 1
        days_held = len(period)
        is_profitable = pnl_amount > 0

        # 收集该持仓期间的所有日收益
        for _, day in period.iterrows():
            rid = day['regime_id']
            r = day['daily_return']
            b = date_to_bench.get(day['date'], 0)
            if pd.isna(r) or pd.isna(b):
                continue
            all_daily_returns[rid].append(r)
            all_bench_returns[rid].append(b)

        # 各状态天数
        state_days = {1: 0, 2: 0, 3: 0, 4: 0}
        for _, day in period.iterrows():
            state_days[day['regime_id']] = state_days.get(day['regime_id'], 0) + 1

        position_stats.append({
            'ticker': ticker, 'buy_date': buy_date, 'sell_date': sell_date,
            'days_held': days_held, 'cost': cost, 'pnl_amount': pnl_amount,
            'pnl_pct': pnl_amount / cost if cost > 0 else 0, 'is_profitable': is_profitable,
            'period_total_return': period_total_return,
            'strong_bull_days': state_days.get(1, 0),
            'weak_bull_days': state_days.get(2, 0),
            'oscillation_days': state_days.get(3, 0),
            'bear_days': state_days.get(4, 0),
        })

    positions_df = pd.DataFrame(position_stats)
    if positions_df.empty:
        return {}, positions_df

    # 按状态汇总：使用平均日收益
    stats = {}
    for rid in [1, 2, 3, 4]:
        name = MarketRegimeDetector.STATE_NAMES[rid]

        # 平均日收益
        strategy_returns = all_daily_returns.get(rid, [])
        bench_returns = all_bench_returns.get(rid, [])

        if not strategy_returns:
            stats[name] = {'positions': 0}
            continue

        avg_strategy_ret = np.mean(strategy_returns)
        avg_bench_ret = np.mean(bench_returns)
        avg_alpha = avg_strategy_ret - avg_bench_ret

        # 日收益胜率：日收益>0的比例
        daily_win_rate = sum(1 for r in strategy_returns if r > 0) / len(strategy_returns) * 100
        # 跑赢基准胜率：日收益>基准日收益的比例
        beat_bench_rate = sum(1 for s, b in zip(strategy_returns, bench_returns) if s > b) / len(strategy_returns) * 100

        # 涉及持仓数
        if rid == 1:
            mask = positions_df['strong_bull_days'] > 0
        elif rid == 2:
            mask = positions_df['weak_bull_days'] > 0
        elif rid == 3:
            mask = positions_df['oscillation_days'] > 0
        else:
            mask = positions_df['bear_days'] > 0

        subset = positions_df[mask]
        total_positions = len(subset)
        total_days = len(strategy_returns)
        win_rate = subset['is_profitable'].sum() / len(subset) * 100 if len(subset) > 0 else 0
        avg_days = subset[['strong_bull_days', 'weak_bull_days', 'oscillation_days', 'bear_days']].sum(axis=1).mean() if len(subset) > 0 else 0

        # 按ticker汇总金额PnL
        ticker_pnl = subset.groupby('ticker')['pnl_amount'].sum().to_dict()
        defense_tickers = set(config.DEFENSE_UNIVERSE.keys())
        equity_tickers = set(config.ETF_UNIVERSE.keys())
        equity_pnl = sum(v for k, v in ticker_pnl.items() if k in equity_tickers)
        defense_pnl = sum(v for k, v in ticker_pnl.items() if k in defense_tickers)

        if ticker_pnl:
            best_t = max(ticker_pnl, key=ticker_pnl.get)
            worst_t = min(ticker_pnl, key=ticker_pnl.get)
            best_name = config.ETF_UNIVERSE.get(best_t, config.DEFENSE_UNIVERSE.get(best_t, best_t))
            worst_name = config.ETF_UNIVERSE.get(worst_t, config.DEFENSE_UNIVERSE.get(worst_t, worst_t))
        else:
            best_t = worst_t = None
            best_name = worst_name = None

        stats[name] = {
            'positions': total_positions,
            'total_days': int(total_days),
            'win_rate': win_rate,
            'daily_win_rate': daily_win_rate,
            'beat_bench_rate': beat_bench_rate,
            'avg_days': avg_days,
            'avg_strategy_ret': avg_strategy_ret,
            'avg_bench_ret': avg_bench_ret,
            'avg_alpha': avg_alpha,
            'equity_pnl': equity_pnl,
            'defense_pnl': defense_pnl,
            'best_ticker': best_t,
            'best_name': best_name,
            'best_pnl': ticker_pnl.get(best_t, 0) if best_t else 0,
            'worst_ticker': worst_t,
            'worst_name': worst_name,
            'worst_pnl': ticker_pnl.get(worst_t, 0) if worst_t else 0,
            'ticker_pnl': ticker_pnl,
        }

    return stats, positions_df


def format_report_v3b(position_stats, positions_df, total_positions, total_days, total_return, total_sharpe, total_mdd, total_trades, num_pairs, total_pnl):
    lines = []

    lines.append("# 市场状态归因分析报告（持仓期间浮盈浮亏 v3b）")
    lines.append(f"\n**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**策略**: ETF轮动 v1.2 基线（16只行业ETF）")
    lines.append(f"**数据口径**: 前复权，统一数据至2026-06-05")
    lines.append(f"**核心口径**: 每笔持仓期间，按持仓期间的市场状态分布归因**平均日收益**")
    lines.append(f"**修正**: 使用平均日收益，避免同时持仓的重复计算问题")
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
    lines.append(f"| 总持仓数 | {total_positions} |")

    lines.append(f"\n## 持仓期间浮盈浮亏归因（平均日收益口径）")
    lines.append(f"\n> **口径说明**：")
    lines.append(f"> - 对每笔持仓（买入→卖出），提取持仓期间的所有交易日")
    lines.append(f"> - 持仓期间日收益 = 该日净值变化 / 前日净值 - 1")
    lines.append(f"> - 按该日所处市场状态归属收益")
    lines.append(f"> - **平均日收益** = 该状态下所有日收益的平均值（避免同时持仓重复计算）")
    lines.append(f"> - 同时提取基准（沪深300）日收益，计算超额")
    lines.append(f"> - 判断：策略持仓期间是否跟上了该状态的大盘动量")

    lines.append(f"\n### 汇总对比")
    lines.append(f"\n| 状态 | 涉及持仓 | 涉及天数 | 持仓胜率 | 日收益胜率 | 跑赢基准率 | 策略平均日收益 | 基准平均日收益 | 平均超额 | 行业ETF PnL | 防御PnL |")
    lines.append(f"|------|----------|----------|----------|------------|------------|----------------|----------------|----------|-------------|----------|")
    for name in ['强牛', '弱牛', '震荡', '熊市']:
        s = position_stats.get(name, {})
        if s.get('positions', 0) == 0:
            lines.append(f"| {name} | 0 | - | - | - | - | - | - | - | - | - |")
            continue
        lines.append(
            f"| {name} | {s['positions']} | {s['total_days']} | {s['win_rate']:.1f}% | "
            f"{s['daily_win_rate']:.1f}% | {s['beat_bench_rate']:.1f}% | "
            f"{s['avg_strategy_ret']*100:.4f}% | {s['avg_bench_ret']*100:.4f}% | {s['avg_alpha']*100:.4f}% | "
            f"{s['equity_pnl']:,.2f} | {s['defense_pnl']:,.2f} |"
        )

    for name in ['强牛', '弱牛', '震荡', '熊市']:
        s = position_stats.get(name, {})
        if s.get('positions', 0) == 0:
            continue
        lines.append(f"\n### {name} 持仓期间详细")
        lines.append(f"- 涉及持仓: {s['positions']} 笔")
        lines.append(f"- 涉及天数: {s['total_days']} 天")
        lines.append(f"- 持仓胜率（PnL>0）: {s['win_rate']:.1f}%")
        lines.append(f"- 日收益胜率（日收益>0）: {s['daily_win_rate']:.1f}%")
        lines.append(f"- 跑赢基准率（日收益>基准）: {s['beat_bench_rate']:.1f}%")
        lines.append(f"- 策略平均日收益: {s['avg_strategy_ret']*100:.4f}%")
        lines.append(f"- 基准平均日收益: {s['avg_bench_ret']*100:.4f}%")
        lines.append(f"- 平均超额（策略-基准）: {s['avg_alpha']*100:.4f}%")
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

    lines.append(f"\n## 交易胜率参考（买入点/卖出点选择）")
    lines.append(f"\n> 口径：每笔配对交易，PnL>0 即盈利。")
    lines.append(f"> - 持仓胜率：持仓期间（买入→卖出）PnL>0 的比率")

    if not positions_df.empty:
        total_win_rate = positions_df['is_profitable'].sum() / len(positions_df) * 100
        lines.append(f"\n- 全区间持仓胜率: {total_win_rate:.1f}% ({positions_df['is_profitable'].sum()}/{len(positions_df)})")

        lines.append(f"\n| 状态 | 持仓数 | 盈利数 | 胜率 | 平均PnL | 平均持有天数 |")
        lines.append(f"|------|--------|--------|------|---------|-------------|")
        for rid in [1, 2, 3, 4]:
            name = MarketRegimeDetector.STATE_NAMES[rid]
            if rid == 1:
                mask = positions_df['strong_bull_days'] > 0
            elif rid == 2:
                mask = positions_df['weak_bull_days'] > 0
            elif rid == 3:
                mask = positions_df['oscillation_days'] > 0
            else:
                mask = positions_df['bear_days'] > 0
            subset = positions_df[mask]
            if len(subset) == 0:
                lines.append(f"| {name} | 0 | - | - | - | - |")
                continue
            wins = subset['is_profitable'].sum()
            rate = wins / len(subset) * 100
            avg_pnl = subset['pnl_amount'].mean()
            avg_days = subset['days_held'].mean()
            lines.append(f"| {name} | {len(subset)} | {wins} | {rate:.1f}% | {avg_pnl:,.2f} | {avg_days:.1f} |")

    lines.append(f"\n## 关键发现")
    lines.append(f"\n1. **强牛持仓**：平均日收益 vs 基准，超额={position_stats.get('强牛', {}).get('avg_alpha', 0)*100:.4f}%，日收益胜率={position_stats.get('强牛', {}).get('daily_win_rate', 0):.1f}%")
    lines.append(f"2. **弱牛持仓**：平均日收益 vs 基准，超额={position_stats.get('弱牛', {}).get('avg_alpha', 0)*100:.4f}%，日收益胜率={position_stats.get('弱牛', {}).get('daily_win_rate', 0):.1f}%")
    lines.append(f"3. **震荡持仓**：平均日收益 vs 基准，超额={position_stats.get('震荡', {}).get('avg_alpha', 0)*100:.4f}%，日收益胜率={position_stats.get('震荡', {}).get('daily_win_rate', 0):.1f}%")
    lines.append(f"4. **熊市持仓**：平均日收益 vs 基准，超额={position_stats.get('熊市', {}).get('avg_alpha', 0)*100:.4f}%，日收益胜率={position_stats.get('熊市', {}).get('daily_win_rate', 0):.1f}%")
    lines.append(f"5. **持仓胜率**：全区间={total_win_rate:.1f}% if not positions_df.empty else 0%，强牛最高={position_stats.get('强牛', {}).get('win_rate', 0):.1f}%")

    lines.append(f"\n## 版本边界")
    lines.append(f"\n- v1.2.2 已收口")
    lines.append(f"- v1.3 研究阶段，持仓期间浮盈浮亏归因（修正平均口径）")
    lines.append(f"- 不改交易规则")
    lines.append(f"- 不设计自适应规则")
    lines.append(f"- 不改市场状态检测算法")

    return '\n'.join(lines)


def main():
    print("="*80)
    print("持仓期间浮盈浮亏归因分析（v3b - 修正平均口径）")
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

    print("\n[5/5] 计算持仓期间浮盈浮亏归因（平均日收益口径）...")
    position_stats, positions_df = calculate_position_period_attribution(pairs_df, nav_regime, bench_df)
    total_positions = len(positions_df) if not positions_df.empty else 0

    print("\n生成报告...")
    report = format_report_v3b(
        position_stats, positions_df,
        total_positions=total_positions,
        total_days=len(nav_df),
        total_return=result['total_return'],
        total_sharpe=result['sharpe_ratio'],
        total_mdd=result['max_drawdown'],
        total_trades=result['num_trades'],
        num_pairs=num_pairs,
        total_pnl=total_pnl,
    )

    report_path = 'reports/market_regime_attribution_v3b.md'
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)
    print(f"\n报告已保存: {report_path}")

    print("\n" + "="*80)
    print("摘要")
    print("="*80)

    for name in ['强牛', '弱牛', '震荡', '熊市']:
        s = position_stats.get(name, {})
        if s.get('positions', 0) > 0:
            print(f"\n{name}:")
            print(f"  涉及持仓: {s['positions']} 笔, {s['total_days']} 天")
            print(f"  持仓胜率: {s['win_rate']:.1f}%")
            print(f"  日收益胜率: {s['daily_win_rate']:.1f}%")
            print(f"  跑赢基准率: {s['beat_bench_rate']:.1f}%")
            print(f"  策略平均日收益: {s['avg_strategy_ret']*100:.4f}%")
            print(f"  基准平均日收益: {s['avg_bench_ret']*100:.4f}%")
            print(f"  平均超额: {s['avg_alpha']*100:.4f}%")
            print(f"  行业ETF: {s['equity_pnl']:,.2f}, 防御: {s['defense_pnl']:,.2f}")

    if not positions_df.empty:
        total_win = positions_df['is_profitable'].sum() / len(positions_df) * 100
        print(f"\n全区间持仓胜率: {total_win:.1f}%")

    print(f"\n[OK] 持仓期间浮盈浮亏归因（修正平均口径）完成")


if __name__ == '__main__':
    main()
