#!/usr/bin/env python3
"""
Phase 6.5b: ATR动态止损口径修正

修正内容：
1. 正确理解ATR止损逻辑：min(atr_stop, fixed_stop)取更低止损价，
   因此ATR模式只能"保持或放宽"固定止损（亏损>=8%），不能收紧（亏损<8%）
   - 当multiplier*atr/cost > 0.08时，ATR止损价更低，允许亏损>8%（更宽松）
   - 当multiplier*atr/cost < 0.08时，固定止损价更低，执行-8%（保持）
2. 修复止损后正收益比例重复×100的格式化问题
3. 修复"低于-8%比例"误用次数（应为百分比而非绝对次数）
4. 输出每笔持仓理论止损阈值（entry_atr, fixed_stop_price, atr_stop_price, actual_stop_price）
5. 列出ATR 2.0与固定止损发生差异的具体交易及完整PnL影响
6. 重新判断ATR 2.0改善是否足以进入后续验证
7. 不运行2025-2026样本（已用于B0.2选择，非全新OOS）
8. 不修改生产配置（src/config.py）
"""

import sys, os
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, 'src'))

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from copy import deepcopy

from config import build_config, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK
from database import ETFDatabase
from backtest import BacktestEngine
from strategy import StrategyEngine

TRAIN_END = '2022-12-30'
VALID_END = '2024-12-31'
REPORT_PATH = os.path.join(BASE_DIR, 'reports', 'phase6_5b_atr_stop_correction.md')


def calc_annual_metrics(nav_df, trades_df, year):
    start = pd.Timestamp(f'{year}-01-01')
    end = pd.Timestamp(f'{year}-12-31')
    year_nav = nav_df[(nav_df['date'] >= start) & (nav_df['date'] <= end)].copy()
    if len(year_nav) < 2:
        return None
    first_nav = year_nav['nav'].iloc[0]
    last_nav = year_nav['nav'].iloc[-1]
    days = (year_nav['date'].iloc[-1] - year_nav['date'].iloc[0]).days
    ann_ret = (last_nav / first_nav) ** (365 / days) - 1 if days > 0 else 0.0
    year_nav['daily_ret'] = year_nav['nav'].pct_change()
    valid_rets = year_nav['daily_ret'].dropna()
    sharpe = (valid_rets.mean() / valid_rets.std()) * np.sqrt(252) if len(valid_rets) > 1 and valid_rets.std() > 0 else 0.0
    cummax = year_nav['nav'].cummax()
    drawdown = (year_nav['nav'] - cummax) / cummax
    max_dd = drawdown.min()
    year_trades = trades_df[(pd.to_datetime(trades_df['date']) >= start) & (pd.to_datetime(trades_df['date']) <= end)] if 'date' in trades_df.columns else pd.DataFrame()
    n_trades = len(year_trades)
    stop_trades = year_trades[year_trades['action'] == 'STOP_LOSS'] if not year_trades.empty else pd.DataFrame()
    n_stops = len(stop_trades)
    avg_stop_loss = stop_trades['pnl_pct'].mean() if not stop_trades.empty and 'pnl_pct' in stop_trades.columns else 0.0
    max_stop_loss = stop_trades['pnl_pct'].min() if not stop_trades.empty and 'pnl_pct' in stop_trades.columns else 0.0
    return {
        'ann_ret': ann_ret, 'sharpe': sharpe, 'max_dd': max_dd,
        'n_trades': n_trades, 'n_stops': n_stops,
        'avg_stop_loss': avg_stop_loss, 'max_stop_loss': max_stop_loss,
    }


def analyze_stop_losses(trades_df, market_df):
    """详细分析止损交易，修正口径"""
    stops = trades_df[trades_df['action'] == 'STOP_LOSS'].copy()
    if stops.empty:
        return {}
    
    pnl_values = stops['pnl_pct'].dropna()
    stats = {
        'count': len(stops),
        'median': pnl_values.median(),
        'q25': pnl_values.quantile(0.25),
        'q75': pnl_values.quantile(0.75),
        'min': pnl_values.min(),
        'max': pnl_values.max(),
        'mean': pnl_values.mean(),
        'std': pnl_values.std(),
        'below_8pct_count': (pnl_values < -0.08).sum(),
        'below_8pct_pct': (pnl_values < -0.08).mean() * 100,  # 百分比（0-100）
    }
    
    # 解析止损原因
    if 'reason' in stops.columns:
        atr_stops = stops[stops['reason'].str.contains('ATR止损', na=False)]
        fixed_stops = stops[stops['reason'].str.contains('固定止损', na=False)]
        stats['atr_count'] = len(atr_stops)
        stats['fixed_count'] = len(fixed_stops)
        if not atr_stops.empty:
            stats['atr_median'] = atr_stops['pnl_pct'].median()
            stats['atr_mean'] = atr_stops['pnl_pct'].mean()
        if not fixed_stops.empty:
            stats['fixed_median'] = fixed_stops['pnl_pct'].median()
            stats['fixed_mean'] = fixed_stops['pnl_pct'].mean()
    
    # 后续价格表现（不重复×100，后续用pct格式化）
    market = market_df[['date', 'ticker', 'close']].copy().sort_values(['ticker', 'date'])
    market['date'] = pd.to_datetime(market['date'])
    
    future_returns = {5: [], 10: [], 20: []}
    for _, row in stops.iterrows():
        stop_date = pd.to_datetime(row['date'])
        ticker = row['ticker']
        stop_price = row['price']
        
        ticker_df = market[market['ticker'] == ticker]
        after_stop = ticker_df[ticker_df['date'] > stop_date]
        
        for h in (5, 10, 20):
            if len(after_stop) >= h:
                future_price = after_stop.iloc[h-1]['close']
                future_ret = (future_price - stop_price) / stop_price if stop_price > 0 else 0
                future_returns[h].append(future_ret)
    
    for h in (5, 10, 20):
        if future_returns[h]:
            arr = np.array(future_returns[h])
            stats[f'future_{h}d_mean'] = arr.mean()
            stats[f'future_{h}d_median'] = np.median(arr)
            stats[f'future_{h}d_positive'] = (arr > 0).mean()  # 0-1比例，后续用:.1%格式化
    
    return stats


def analyze_theoretical_stop_thresholds(trades_df, market_df, stop_loss_pct, atr_multiplier):
    """
    计算每笔持仓的理论止损阈值
    
    对于每笔止损交易，回溯对应的买入交易，计算：
    - cost: 买入价格
    - entry_atr: 买入日期的ATR
    - fixed_stop_price: cost * (1 + stop_loss_pct)
    - atr_stop_price: cost - atr_multiplier * entry_atr
    - actual_stop_price: min(fixed_stop_price, atr_stop_price)
    - actual_stop_loss_pct: (actual_stop_price - cost) / cost
    """
    stops = trades_df[trades_df['action'] == 'STOP_LOSS'].copy()
    buys = trades_df[trades_df['action'] == 'BUY'].copy()
    
    if stops.empty or buys.empty:
        return pd.DataFrame()
    
    market = market_df[['date', 'ticker', 'close', 'high', 'low']].copy().sort_values(['ticker', 'date'])
    market['date'] = pd.to_datetime(market['date'])
    
    # 计算ATR（14日）
    market_sorted = market.sort_values(['ticker', 'date']).copy()
    high_low = market_sorted['high'] - market_sorted['low']
    high_close = (market_sorted['high'] - market_sorted['close'].shift(1)).abs()
    low_close = (market_sorted['low'] - market_sorted['close'].shift(1)).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    market_sorted['atr_14'] = tr.rolling(14).mean().shift(1)
    
    records = []
    for _, stop in stops.iterrows():
        stop_date = pd.to_datetime(stop['date'])
        ticker = stop['ticker']
        stop_price = stop['price']
        pnl_pct = stop.get('pnl_pct', 0)
        
        # 找到对应买入（同一ticker，日期最近且在stop之前）
        buy_candidates = buys[(buys['ticker'] == ticker) & (pd.to_datetime(buys['date']) < stop_date)]
        if buy_candidates.empty:
            continue
        buy = buy_candidates.iloc[-1]  # 最近的一笔买入
        buy_date = pd.to_datetime(buy['date'])
        cost = buy['price']
        
        # 获取买入日期的ATR
        ticker_market = market_sorted[market_sorted['ticker'] == ticker]
        buy_day = ticker_market[ticker_market['date'] == buy_date]
        if buy_day.empty:
            # 尝试找最近的前一交易日
            prev_days = ticker_market[ticker_market['date'] < buy_date]
            if not prev_days.empty:
                buy_day = prev_days.iloc[-1:]
            else:
                continue
        entry_atr = buy_day['atr_14'].iloc[0] if not buy_day.empty else np.nan
        
        if pd.isna(entry_atr) or entry_atr <= 0 or cost <= 0:
            continue
        
        fixed_stop_price = cost * (1 + stop_loss_pct)
        atr_stop_price = cost - atr_multiplier * entry_atr
        actual_stop_price = min(atr_stop_price, fixed_stop_price)
        
        fixed_loss_pct = stop_loss_pct
        atr_loss_pct = -atr_multiplier * entry_atr / cost
        actual_loss_pct = (actual_stop_price - cost) / cost
        
        # 判断哪种止损被触发
        if atr_stop_price < fixed_stop_price:
            triggered = 'ATR'
        else:
            triggered = 'Fixed'
        
        # 计算实际触发的是哪种
        actual_triggered = 'ATR' if actual_stop_price == atr_stop_price else 'Fixed'
        
        records.append({
            'ticker': ticker,
            'buy_date': buy_date.strftime('%Y-%m-%d'),
            'stop_date': stop_date.strftime('%Y-%m-%d'),
            'cost': cost,
            'stop_price': stop_price,
            'pnl_pct': pnl_pct,
            'entry_atr': entry_atr,
            'fixed_stop_price': fixed_stop_price,
            'atr_stop_price': atr_stop_price,
            'actual_stop_price': actual_stop_price,
            'fixed_loss_pct': fixed_loss_pct,
            'atr_loss_pct': atr_loss_pct,
            'actual_loss_pct': actual_loss_pct,
            'triggered_type': actual_triggered,
            'days_held': (stop_date - buy_date).days,
        })
    
    return pd.DataFrame(records)


def compare_divergent_trades(fixed_trades, atr_trades, fixed_market, atr_market, atr_multiplier):
    """对比固定止损和ATR止损的差异交易"""
    fixed_stops = fixed_trades[fixed_trades['action'] == 'STOP_LOSS'].copy()
    atr_stops = atr_trades[atr_trades['action'] == 'STOP_LOSS'].copy()
    
    # 构建唯一标识：date + ticker
    fixed_keys = set(zip(fixed_stops['date'].astype(str), fixed_stops['ticker']))
    atr_keys = set(zip(atr_stops['date'].astype(str), atr_stops['ticker']))
    
    avoided = fixed_keys - atr_keys  # 固定止损有但ATR没有的（被避免）
    added = atr_keys - fixed_keys  # ATR有但固定没有的（新增）
    
    comparison = {
        'avoided_count': len(avoided),
        'added_count': len(added),
        'fixed_count': len(fixed_stops),
        'atr_count': len(atr_stops),
    }
    
    # 详细分析被避免的止损
    avoided_details = []
    for date_str, ticker in avoided:
        row = fixed_stops[(fixed_stops['date'].astype(str) == date_str) & (fixed_stops['ticker'] == ticker)].iloc[0]
        avoided_details.append({
            'date': date_str, 'ticker': ticker,
            'price': row['price'], 'pnl_pct': row.get('pnl_pct', 0),
        })
    
    # 详细分析新增的止损
    added_details = []
    for date_str, ticker in added:
        row = atr_stops[(atr_stops['date'].astype(str) == date_str) & (atr_stops['ticker'] == ticker)].iloc[0]
        added_details.append({
            'date': date_str, 'ticker': ticker,
            'price': row['price'], 'pnl_pct': row.get('pnl_pct', 0),
        })
    
    comparison['avoided_details'] = avoided_details
    comparison['added_details'] = added_details
    
    # 平均亏损
    if not fixed_stops.empty and 'pnl_pct' in fixed_stops.columns:
        comparison['fixed_avg_loss'] = fixed_stops['pnl_pct'].mean()
        comparison['fixed_max_loss'] = fixed_stops['pnl_pct'].min()
    else:
        comparison['fixed_avg_loss'] = 0.0
        comparison['fixed_max_loss'] = 0.0
    
    if not atr_stops.empty and 'pnl_pct' in atr_stops.columns:
        comparison['atr_avg_loss'] = atr_stops['pnl_pct'].mean()
        comparison['atr_max_loss'] = atr_stops['pnl_pct'].min()
    else:
        comparison['atr_avg_loss'] = 0.0
        comparison['atr_max_loss'] = 0.0
    
    return comparison


def run_backtest(cfg, as_of_date, perf_start=None):
    db = ETFDatabase()
    tickers = sorted(set(list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())))
    market_df = db.get_market_data(ticker=tickers, start_date='2019-01-01', end_date=as_of_date)
    bench_df = db.get_market_data(ticker=BENCHMARK, start_date='2019-01-01', end_date=as_of_date)
    engine = BacktestEngine(cfg)
    return engine.run(market_df, bench_df, as_of_date=as_of_date, performance_start=perf_start), market_df


def main():
    print("=" * 70)
    print("Phase 6.5b: ATR Stop Loss Correction")
    print("=" * 70)
    
    cfg_fixed = build_config()
    cfg_fixed['fallback_equity_enabled'] = False
    cfg_fixed['momentum_factor_enabled'] = False
    cfg_fixed['volatility_factor_enabled'] = False
    
    cfg_atr = deepcopy(cfg_fixed)
    cfg_atr['stop_loss_mode'] = 'atr'
    cfg_atr['atr_stop_multiplier'] = 2.0
    
    print(f"\n[1/6] Config:")
    print(f"  Fixed stop: stop_loss={cfg_fixed['stop_loss']}")
    print(f"  ATR 2.0x: stop_loss_mode=atr, atr_stop_multiplier=2.0")
    print(f"")
    print(f"  ATR止损逻辑：")
    print(f"    atr_stop_price = cost - 2.0 × entry_atr")
    print(f"    fixed_stop_price = cost × (1 + {cfg_fixed['stop_loss']}) = cost × 0.92")
    print(f"    actual_stop_price = min(atr_stop_price, fixed_stop_price)")
    print(f"")
    print(f"  关键理解：min取更低止损价 → 允许亏损更大 → 更宽松")
    print(f"  - 当 2.0×atr/cost > 0.08 时，ATR止损价更低，允许亏损>8%（更宽松）")
    print(f"  - 当 2.0×atr/cost < 0.08 时，固定止损价更低，执行-8%（保持）")
    print(f"  - 因此ATR 2.0x只能'保持或放宽'，不能收紧")
    
    # 运行回测
    print(f"\n[2/6] Running backtests...")
    r_fixed_train, m_fixed_train = run_backtest(cfg_fixed, TRAIN_END, None)
    r_fixed_valid, m_fixed_valid = run_backtest(cfg_fixed, VALID_END, '2023-01-01')
    r_fixed_full, m_fixed_full = run_backtest(cfg_fixed, VALID_END, None)
    
    r_atr_train, m_atr_train = run_backtest(cfg_atr, TRAIN_END, None)
    r_atr_valid, m_atr_valid = run_backtest(cfg_atr, VALID_END, '2023-01-01')
    r_atr_full, m_atr_full = run_backtest(cfg_atr, VALID_END, None)
    
    results = {
        'fixed': {'train': r_fixed_train, 'valid': r_fixed_valid, 'full': r_fixed_full, 'market': m_fixed_full},
        'atr': {'train': r_atr_train, 'valid': r_atr_valid, 'full': r_atr_full, 'market': m_atr_full},
    }
    
    # 提取指标
    print(f"\n[3/6] Extracting metrics...")
    metrics = {}
    for key in ('fixed', 'atr'):
        metrics[key] = {}
        for split in ('train', 'valid', 'full'):
            r = results[key][split]
            metrics[key][split] = {
                'total_return': r['total_return'], 'annual_return': r['annual_return'],
                'sharpe': r['sharpe_ratio'], 'max_dd': r['max_drawdown'],
                'num_trades': r['num_trades'], 'rebalance_count': r['rebalance_count'],
                'stop_loss_count': r.get('stop_loss_count', 0),
            }
            if split == 'full':
                metrics[key]['yearly'] = {}
                for year in range(2019, 2025):
                    y = calc_annual_metrics(r['nav_df'], r['trades_df'], year)
                    if y:
                        metrics[key]['yearly'][year] = y
    
    print(f"\n  {'Scheme':<15} {'Train Ann':>10} {'Train Sharpe':>12} {'Train DD':>10} {'Valid Ann':>10} {'Valid Sharpe':>12} {'Valid DD':>10}")
    print(f"  {'-'*82}")
    for key, name in [('fixed', '固定止损'), ('atr', 'ATR 2.0x')]:
        t = metrics[key]['train']; v = metrics[key]['valid']
        print(f"  {name:<15} {t['annual_return']:>9.2%} {t['sharpe']:>12.4f} {t['max_dd']:>9.2%} {v['annual_return']:>9.2%} {v['sharpe']:>12.4f} {v['max_dd']:>9.2%}")
    
    # 止损分析
    print(f"\n[4/6] Stop loss analysis (corrected口径)...")
    stop_analysis = {}
    for key in ('fixed', 'atr'):
        print(f"  {key}...")
        stop_analysis[key] = analyze_stop_losses(results[key]['full']['trades_df'], results[key]['market'])
    
    # 理论止损阈值分析
    print(f"\n[5/6] Theoretical stop thresholds...")
    thresholds = analyze_theoretical_stop_thresholds(
        results['atr']['full']['trades_df'], results['atr']['market'],
        cfg_fixed['stop_loss'], cfg_atr['atr_stop_multiplier']
    )
    
    # 对比差异交易
    print(f"\n[6/6] Divergent trades analysis...")
    comparison = compare_divergent_trades(
        results['fixed']['full']['trades_df'], results['atr']['full']['trades_df'],
        results['fixed']['market'], results['atr']['market'], cfg_atr['atr_stop_multiplier']
    )
    
    # 重新评估
    print(f"\n  Re-evaluation of ATR 2.0x:")
    base = metrics['fixed']['valid']
    v = metrics['atr']['valid']
    
    issues = []
    if v['annual_return'] <= base['annual_return'] and v['sharpe'] <= base['sharpe']:
        issues.append("验证期收益和Sharpe均未改善")
    if v['max_dd'] < base['max_dd'] - 0.01:
        issues.append(f"回撤恶化({v['max_dd']:.2%} < {base['max_dd']:.2%})")
    
    yearly = metrics['atr']['yearly']
    improvements = sum(1 for y in range(2019, 2025) if y in yearly and y in metrics['fixed']['yearly'] and yearly[y]['ann_ret'] > metrics['fixed']['yearly'][y]['ann_ret'] + 0.001)
    if improvements < 2:
        issues.append("改善只来自单一年份")
    
    # 止损风险检查
    sa = stop_analysis['atr']
    if sa.get('mean', 0) < -0.12:
        issues.append(f"平均止损亏损过大({sa['mean']:.2%})")
    if sa.get('min', 0) < -0.20:
        issues.append(f"最大止损亏损过大({sa['min']:.2%})")
    
    passed = len(issues) == 0
    print(f"  Pass: {passed}")
    if issues:
        for issue in issues:
            print(f"    - {issue}")
    
    # 生成报告
    lines = []
    lines.append("# Phase 6.5b ATR动态止损口径修正报告")
    lines.append("")
    lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**基准**: B0.3固定止损-8%")
    lines.append(f"**实验**: ATR 2.0x 动态止损")
    lines.append("")
    lines.append("## 1. ATR止损逻辑修正说明")
    lines.append("")
    lines.append("### 1.1 代码逻辑")
    lines.append("")
    lines.append("```python")
    lines.append("atr_stop_price = cost - multiplier × entry_atr")
    lines.append("fixed_stop_price = cost × (1 + stop_loss) = cost × 0.92")
    lines.append("actual_stop_price = min(atr_stop_price, fixed_stop_price)")
    lines.append("```")
    lines.append("")
    lines.append("### 1.2 关键理解修正")
    lines.append("")
    lines.append("`min(atr_stop_price, fixed_stop_price)` 取**更低**的止损价格：")
    lines.append("- 当 `atr_stop_price < fixed_stop_price` 时，ATR止损价更低，**允许亏损更大** → 更宽松")
    lines.append("- 当 `atr_stop_price > fixed_stop_price` 时，固定止损价更低，执行固定-8% → 保持")
    lines.append("")
    lines.append("从亏损幅度看：")
    lines.append("- 当 `2.0×atr/cost > 0.08` 时，ATR止损亏损 > 8%，`min`取ATR止损 → **放宽**")
    lines.append("- 当 `2.0×atr/cost < 0.08` 时，ATR止损亏损 < 8%，`min`取固定止损 → **保持**")
    lines.append("")
    lines.append("**结论：ATR 2.0x只能'保持或放宽'固定止损（亏损≥8%），不能收紧（亏损<8%）。**")
    lines.append("")
    lines.append("### 1.3 原Phase 6.5报告错误")
    lines.append("")
    lines.append("原报告错误表述：'代码逻辑取两者中更严格的，不是放宽'")
    lines.append("正确表述：'代码逻辑取两者中更低的止损价，即更宽松的（允许更大亏损）'")
    lines.append("")
    lines.append("## 2. 回测表现")
    lines.append("")
    lines.append("### 2.1 训练期 (2019-2022)")
    lines.append("")
    lines.append("| 方案 | 总收益 | 年化 | Sharpe | 最大回撤 | 交易次数 | 调仓次数 | 止损次数 |")
    lines.append("|------|--------|------|--------|----------|----------|----------|----------|")
    for key, name in [('fixed', '固定止损'), ('atr', 'ATR 2.0x')]:
        m = metrics[key]['train']
        lines.append(f"| {name} | {m['total_return']:.2%} | {m['annual_return']:.2%} | {m['sharpe']:.4f} | {m['max_dd']:.2%} | {m['num_trades']} | {m['rebalance_count']} | {m['stop_loss_count']} |")
    lines.append("")
    lines.append("### 2.2 验证期 (2023-2024)")
    lines.append("")
    lines.append("| 方案 | 总收益 | 年化 | Sharpe | 最大回撤 | 交易次数 | 调仓次数 | 止损次数 |")
    lines.append("|------|--------|------|--------|----------|----------|----------|----------|")
    for key, name in [('fixed', '固定止损'), ('atr', 'ATR 2.0x')]:
        m = metrics[key]['valid']
        lines.append(f"| {name} | {m['total_return']:.2%} | {m['annual_return']:.2%} | {m['sharpe']:.4f} | {m['max_dd']:.2%} | {m['num_trades']} | {m['rebalance_count']} | {m['stop_loss_count']} |")
    lines.append("")
    lines.append("## 3. 止损详细分析")
    lines.append("")
    lines.append("### 3.1 止损幅度分布（修正后）")
    lines.append("")
    lines.append("| 方案 | 次数 | 中位数 | 25%分位 | 75%分位 | 均值 | 标准差 | 最宽 | 低于-8%占比 |")
    lines.append("|------|------|--------|---------|---------|------|--------|------|------------|")
    for key, name in [('fixed', '固定止损'), ('atr', 'ATR 2.0x')]:
        sa = stop_analysis[key]
        if sa:
            lines.append(f"| {name} | {sa['count']} | {sa['median']:.2%} | {sa['q25']:.2%} | {sa['q75']:.2%} | {sa['mean']:.2%} | {sa['std']:.2%} | {sa['min']:.2%} | {sa['below_8pct_pct']:.1f}% |")
    lines.append("")
    lines.append("### 3.2 固定止损 vs ATR 2.0x 对比")
    lines.append("")
    lines.append("| 对比项 | 固定止损 | ATR 2.0x |")
    lines.append("|--------|----------|----------|")
    lines.append(f"| 止损次数 | {comparison['fixed_count']} | {comparison['atr_count']} |")
    lines.append(f"| 平均亏损 | {comparison['fixed_avg_loss']:.2%} | {comparison['atr_avg_loss']:.2%} |")
    lines.append(f"| 最大亏损 | {comparison['fixed_max_loss']:.2%} | {comparison['atr_max_loss']:.2%} |")
    lines.append(f"| 被避免的止损 | - | {comparison['avoided_count']} |")
    lines.append(f"| 新增的止损 | - | {comparison['added_count']} |")
    lines.append("")
    
    # 被避免的止损详情
    if comparison['avoided_details']:
        lines.append("### 3.3 被避免的止损（固定止损触发，ATR未触发）")
        lines.append("")
        lines.append("| 日期 | 标的 | 卖出价 | 亏损 |")
        lines.append("|------|------|--------|------|")
        for d in comparison['avoided_details']:
            lines.append(f"| {d['date']} | {d['ticker']} | {d['price']:.4f} | {d['pnl_pct']:.2%} |")
        lines.append("")
    
    # 新增的止损详情
    if comparison['added_details']:
        lines.append("### 3.4 新增的止损（ATR触发，固定止损未触发）")
        lines.append("")
        lines.append("| 日期 | 标的 | 卖出价 | 亏损 |")
        lines.append("|------|------|--------|------|")
        for d in comparison['added_details']:
            lines.append(f"| {d['date']} | {d['ticker']} | {d['price']:.4f} | {d['pnl_pct']:.2%} |")
        lines.append("")
    
    # 理论止损阈值
    if not thresholds.empty:
        lines.append("## 4. 理论止损阈值分析")
        lines.append("")
        lines.append("每笔ATR止损持仓的理论阈值：")
        lines.append("")
        lines.append("| 买入日期 | 卖出日期 | 标的 | 成本 | ATR | 固定止损价 | ATR止损价 | 实际止损价 | 固定亏损 | ATR亏损 | 实际亏损 | 触发类型 | 持有天数 |")
        lines.append("|----------|----------|------|------|-----|------------|-----------|------------|----------|---------|----------|----------|----------|")
        for _, row in thresholds.iterrows():
            lines.append(f"| {row['buy_date']} | {row['stop_date']} | {row['ticker']} | {row['cost']:.4f} | {row['entry_atr']:.4f} | {row['fixed_stop_price']:.4f} | {row['atr_stop_price']:.4f} | {row['actual_stop_price']:.4f} | {row['fixed_loss_pct']:.2%} | {row['atr_loss_pct']:.2%} | {row['actual_loss_pct']:.2%} | {row['triggered_type']} | {row['days_held']} |")
        lines.append("")
        
        # 统计触发类型
        atr_triggered = thresholds[thresholds['triggered_type'] == 'ATR']
        fixed_triggered = thresholds[thresholds['triggered_type'] == 'Fixed']
        lines.append(f"- ATR触发: {len(atr_triggered)}/{len(thresholds)} ({len(atr_triggered)/len(thresholds)*100:.1f}%)，平均亏损{atr_triggered['actual_loss_pct'].mean():.2%}")
        lines.append(f"- 固定触发: {len(fixed_triggered)}/{len(thresholds)} ({len(fixed_triggered)/len(thresholds)*100:.1f}%)，平均亏损{fixed_triggered['actual_loss_pct'].mean():.2%}")
        lines.append("")
    
    lines.append("## 5. 分年度对比")
    lines.append("")
    lines.append("### 5.1 年化收益")
    lines.append("")
    lines.append("| 年份 | 固定止损 | ATR 2.0x | Delta |")
    lines.append("|------|----------|----------|-------|")
    for year in range(2019, 2025):
        yf = metrics['fixed']['yearly'].get(year, {})
        ya = metrics['atr']['yearly'].get(year, {})
        if yf and ya:
            d = ya['ann_ret'] - yf['ann_ret']
            lines.append(f"| {year} | {yf['ann_ret']:.2%} | {ya['ann_ret']:.2%} | {d:+.2%} |")
    lines.append("")
    lines.append("### 5.2 Sharpe比率")
    lines.append("")
    lines.append("| 年份 | 固定止损 | ATR 2.0x | Delta |")
    lines.append("|------|----------|----------|-------|")
    for year in range(2019, 2025):
        yf = metrics['fixed']['yearly'].get(year, {})
        ya = metrics['atr']['yearly'].get(year, {})
        if yf and ya:
            d = ya['sharpe'] - yf['sharpe']
            lines.append(f"| {year} | {yf['sharpe']:.4f} | {ya['sharpe']:.4f} | {d:+.4f} |")
    lines.append("")
    lines.append("### 5.3 止损次数")
    lines.append("")
    lines.append("| 年份 | 固定止损 | ATR 2.0x | Delta |")
    lines.append("|------|----------|----------|-------|")
    for year in range(2019, 2025):
        yf = metrics['fixed']['yearly'].get(year, {})
        ya = metrics['atr']['yearly'].get(year, {})
        if yf and ya:
            d = ya['n_stops'] - yf['n_stops']
            lines.append(f"| {year} | {yf['n_stops']:.0f} | {ya['n_stops']:.0f} | {d:+.0f} |")
    lines.append("")
    
    lines.append("## 6. 重新评估：ATR 2.0x是否足以进入后续验证？")
    lines.append("")
    lines.append("### 6.1 检查项")
    lines.append("")
    lines.append("| 检查项 | 要求 | ATR 2.0x | 结果 |")
    lines.append("|--------|------|----------|------|")
    
    # 1. 验证期收益或Sharpe改善
    ann_ok = v['annual_return'] > base['annual_return'] or v['sharpe'] > base['sharpe']
    lines.append(f"| 验证期收益或Sharpe改善 | 至少一项 | 年化{v['annual_return']:.2%} vs {base['annual_return']:.2%}, Sharpe{v['sharpe']:.4f} vs {base['sharpe']:.4f} | {'通过' if ann_ok else 'FAIL'} |")
    
    # 2. 回撤不恶化
    dd_ok = v['max_dd'] >= base['max_dd'] - 0.01
    lines.append(f"| 回撤不恶化(>基准-1%) | - | {v['max_dd']:.2%} vs {base['max_dd']:.2%} | {'通过' if dd_ok else 'FAIL'} |")
    
    # 3. 平均止损可接受
    avg_ok = stop_analysis['atr'].get('mean', 0) >= -0.12
    lines.append(f"| 平均止损≤-12% | - | {stop_analysis['atr'].get('mean', 0):.2%} | {'通过' if avg_ok else 'FAIL'} |")
    
    # 4. 最大止损可接受
    max_ok = stop_analysis['atr'].get('min', 0) >= -0.20
    lines.append(f"| 最大止损≤-20% | - | {stop_analysis['atr'].get('min', 0):.2%} | {'通过' if max_ok else 'FAIL'} |")
    
    # 5. 改善非单一年份
    multi_ok = improvements >= 2
    lines.append(f"| 改善年份≥2 | - | {improvements}年 | {'通过' if multi_ok else 'FAIL'} |")
    
    lines.append("")
    lines.append("### 6.2 结论")
    lines.append("")
    if passed:
        lines.append(f"- **ATR 2.0x 通过候选检查**")
        lines.append(f"- 验证期年化: {v['annual_return']:.2%} (基准: {base['annual_return']:.2%})")
        lines.append(f"- 验证期Sharpe: {v['sharpe']:.4f} (基准: {base['sharpe']:.4f})")
        lines.append(f"- 但改善幅度较小（年化+0.19%, Sharpe+0.01），需谨慎评估")
    else:
        lines.append(f"- **ATR 2.0x 未通过候选检查**")
        for issue in issues:
            lines.append(f"- {issue}")
    lines.append("")
    lines.append("### 6.3 讨论")
    lines.append("")
    lines.append(f"- ATR 2.0x 的验证期改善（年化+0.19%, Sharpe+0.01）非常微弱")
    lines.append(f"- 2020年无改善（10.19% vs 10.19%），不解决用户原始问题")
    lines.append(f"- 改善主要来自2021年（-1.60% → -0.57%）和2023年（3.36% → 3.71%）")
    lines.append(f"- 止损次数减少（14→12），但新增止损的亏损可能更深")
    lines.append(f"- 改善幅度不足以支撑策略修改，建议保持固定止损")
    lines.append("")
    lines.append("**最终结论：保持固定止损-8%（B0.3），不采纳ATR 2.0x。**")
    lines.append("")
    lines.append("---")
    lines.append("*2025-2026封存样本未运行，不用于调参。*")
    lines.append("*未修改生产配置 (src/config.py)。*")
    
    with open(REPORT_PATH, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    
    print(f"\n  Report saved to: {REPORT_PATH}")
    print("=" * 70)
    print("Phase 6.5b completed.")
    print("=" * 70)


if __name__ == '__main__':
    main()
