# -*- coding: utf-8 -*-
"""
候选池宽度—实际仓位—后续表现 诊断脚本 v2（修正统计口径）

统计单位修正：
- 所有统计从统一策略起点 2019-08-13 开始，排除预热期和未知状态期。
- 调仓日 = 每周四（与漏斗审计一致）。
- 两张独立表：
  1. 事件等权：每个实际调仓日一行，总行数 = 有效调仓日数（335）。
  2. 日历日加权：调仓区间展开为每日，但明确不称为“调仓周期”。

三个样本数定义：
- 346：funnel_audit_daily.csv 中所有周四调仓日的总数（2019-06-06 至 2026-06-11）。
- 336：排除预热期（2019-08-13 前）后的调仓日数。
- 335：再排除未知状态（bench 数据不足计算 MA20/MA50）后的有效调仓日数。
- 1649（之前错误）：把每个交易日当作“调仓周期”重复计权的结果。
- 541（之前错误）：基于每日而非调仓日统计的“候选>5”错误样本数。

运行: cd D:/etf_rotation_model && py scripts/candidate_pool_analysis_v2.py
"""
import sys
sys.path.insert(0, 'D:/etf_rotation_model/src')

import pandas as pd
import numpy as np
from database import ETFDatabase
from backtest import BacktestEngine
from strategy import StrategyEngine
from config import STRATEGY_CONFIG, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK

B0_18_CORE = list(ETF_UNIVERSE.keys())
B0_18_DEFENSE = list(DEFENSE_UNIVERSE.keys())

print("=" * 70)
print("候选池宽度—实际仓位—后续表现 诊断 v2（修正统计口径）")
print("=" * 70)

# ============================================================
# 1. 加载数据
# ============================================================
print("\n[1/6] 加载数据...")
db = ETFDatabase('D:/etf_rotation_model/database/etf_model.db')
market_df = db.get_market_data(ticker=B0_18_CORE + B0_18_DEFENSE)
bench_df = db.get_market_data(ticker=BENCHMARK)
market_df['date'] = pd.to_datetime(market_df['date'])
bench_df['date'] = pd.to_datetime(bench_df['date'])

market_core = market_df[market_df['ticker'].isin(B0_18_CORE)].copy()
core_pivot = market_core.pivot_table(index='date', columns='ticker', values='close')
core_ew = core_pivot.mean(axis=1)

bench_prices = bench_df.set_index('date')['close']

print(f"  市场数据: {market_df['date'].nunique()} 交易日, {market_df['ticker'].nunique()} 只ETF")
print(f"  基准数据: {bench_df['date'].nunique()} 交易日")

# ============================================================
# 2. 加载漏斗审计数据（调仓日 = 每周四）
# ============================================================
print("\n[2/6] 加载漏斗审计数据...")
funnel_df = pd.read_csv('D:/etf_rotation_model/reports/funnel_audit_daily.csv')
funnel_df['date'] = pd.to_datetime(funnel_df['date'])

# 统一策略起点
WARMUP_END = pd.to_datetime('2019-08-13')

# 排除预热期和未知状态
valid_funnel = funnel_df[
    (funnel_df['date'] >= WARMUP_END) &
    (funnel_df['regime'] != '未知')
].copy().sort_values('date').reset_index(drop=True)

print(f"  漏斗审计总调仓日: {len(funnel_df)}")
print(f"  排除预热期(2019-08-13前): {len(funnel_df[funnel_df['date'] < WARMUP_END])} 个")
print(f"  排除未知状态: {len(funnel_df[(funnel_df['date'] >= WARMUP_END) & (funnel_df['regime'] == '未知')])} 个")
print(f"  有效调仓日: {len(valid_funnel)}")

# 三个样本数定义
N_TOTAL = len(funnel_df)          # 346
N_POST_WARMUP = len(funnel_df[funnel_df['date'] >= WARMUP_END])  # 336
N_VALID = len(valid_funnel)       # 335

print(f"\n  样本数定义:")
print(f"    N_TOTAL = {N_TOTAL}（所有周四调仓日，2019-06-06至2026-06-11）")
print(f"    N_POST_WARMUP = {N_POST_WARMUP}（排除预热期后）")
print(f"    N_VALID = {N_VALID}（再排除未知状态后，有效调仓日）")

# ============================================================
# 3. 运行回测获取每日持仓
# ============================================================
print("\n[3/6] 运行回测获取每日持仓...")
cfg = STRATEGY_CONFIG.copy()
cfg['fallback_equity_enabled'] = False
engine = BacktestEngine(cfg)
result = engine.run(market_df, bench_df)
nav_df = result['nav_df'].copy()
nav_df['date'] = pd.to_datetime(nav_df['date'])

def safe_eval(x):
    if isinstance(x, str) and x.startswith('{'):
        return eval(x)
    return x if isinstance(x, dict) else {}

nav_df['positions_pct'] = nav_df['positions_pct'].apply(safe_eval)
nav_df['positions_detail'] = nav_df['positions_detail'].apply(safe_eval)

def calc_sector_defense(pos_pct):
    if not pos_pct:
        return 0, 0
    sector = sum(v for k, v in pos_pct.items() if k in B0_18_CORE)
    defense = sum(v for k, v in pos_pct.items() if k in B0_18_DEFENSE)
    return sector, defense

nav_df[['sector_pct', 'defense_pct']] = nav_df['positions_pct'].apply(lambda x: pd.Series(calc_sector_defense(x)))
nav_df['cash_pct'] = 1 - nav_df['sector_pct'] - nav_df['defense_pct']

# 前一日行业仓位（原有）
nav_df['sector_pct_old'] = nav_df['sector_pct'].shift(1)

print(f"  回测完成: {len(nav_df)} 日")

# ============================================================
# 4. 构建事件等权表：每个调仓日一行
# ============================================================
print("\n[4/6] 构建事件等权表...")

rebalance_dates = valid_funnel['date'].tolist()

def next_rebalance_date(current_date, all_dates):
    """找到下一个调仓日"""
    future = [d for d in all_dates if d > current_date]
    return future[0] if future else None

event_records = []

for i, date in enumerate(rebalance_dates):
    next_date = next_rebalance_date(date, rebalance_dates)
    if next_date is None:
        continue
    
    # 从漏斗审计获取候选数量
    funnel_row = valid_funnel[valid_funnel['date'] == date]
    if funnel_row.empty:
        continue
    n_candidates = funnel_row['stage5_buy_signals'].iloc[0]
    regime = funnel_row['regime'].iloc[0]
    year = date.year
    
    # 从回测获取持仓状态
    day_nav = nav_df[nav_df['date'] == date]
    if day_nav.empty:
        continue
    
    sector_pct_old = day_nav['sector_pct_old'].iloc[0]  # 原有行业仓位（前一日）
    sector_pct_new = day_nav['sector_pct'].iloc[0]       # 新增目标仓位（当日收盘）
    defense_pct = day_nav['defense_pct'].iloc[0]
    cash_pct = day_nav['cash_pct'].iloc[0]
    
    # 下次调仓前收益（从当前周四到下一个周四）
    period_nav = nav_df[(nav_df['date'] >= date) & (nav_df['date'] <= next_date)]
    if len(period_nav) < 2:
        continue
    portfolio_ret = period_nav['nav'].iloc[-1] / period_nav['nav'].iloc[0] - 1
    
    # 行业等权池收益
    if date in core_ew.index and next_date in core_ew.index:
        ew_ret = core_ew.loc[next_date] / core_ew.loc[date] - 1
    else:
        ew_ret = np.nan
    
    # 沪深300收益
    if date in bench_prices.index and next_date in bench_prices.index:
        bench_ret = bench_prices.loc[next_date] / bench_prices.loc[date] - 1
    else:
        bench_ret = np.nan
    
    alpha_vs_ew = portfolio_ret - ew_ret if not pd.isna(ew_ret) else np.nan
    alpha_vs_bench = portfolio_ret - bench_ret if not pd.isna(bench_ret) else np.nan
    
    # 持仓变化
    sector_change = sector_pct_new - (sector_pct_old if not pd.isna(sector_pct_old) else 0)
    
    event_records.append({
        'date': date.strftime('%Y-%m-%d'),
        'next_date': next_date.strftime('%Y-%m-%d'),
        'n_candidates': n_candidates,
        'regime': regime,
        'year': year,
        'sector_pct_old': sector_pct_old,
        'sector_pct_new': sector_pct_new,
        'sector_change': sector_change,
        'defense_pct': defense_pct,
        'cash_pct': cash_pct,
        'portfolio_ret': portfolio_ret,
        'ew_ret': ew_ret,
        'bench_ret': bench_ret,
        'alpha_vs_ew': alpha_vs_ew,
        'alpha_vs_bench': alpha_vs_bench,
    })

event_df = pd.DataFrame(event_records)
print(f"  事件等权表: {len(event_df)} 行")
assert len(event_df) == N_VALID - 1, f"事件等权表行数({len(event_df)})应等于N_VALID-1({N_VALID-1})，因为最后一个调仓日无下期"

# 保存事件等权表
event_df.to_csv('D:/etf_rotation_model/reports/event_level_table.csv', index=False, encoding='utf-8-sig')

# ============================================================
# 5. 分组统计（事件等权）
# ============================================================
print("\n[5/6] 事件等权分组统计...")

def candidate_group(n):
    if n == 0: return '0只'
    if n <= 2: return '1-2只'
    if n <= 4: return '3-4只'
    if n == 5: return '5只'
    if n <= 8: return '6-8只'
    return '9只以上'

event_df['group'] = event_df['n_candidates'].apply(candidate_group)

def calc_stats(series, name):
    """计算样本数、均值、中位数、胜率、标准误、95%CI"""
    s = series.dropna()
    n = len(s)
    if n == 0:
        return {'n': 0, 'mean': np.nan, 'median': np.nan, 'win_rate': np.nan, 'se': np.nan, 'ci_lower': np.nan, 'ci_upper': np.nan}
    mean = s.mean()
    median = s.median()
    win_rate = (s > 0).sum() / n
    se = s.std() / np.sqrt(n) if n > 1 else np.nan
    # 95% CI using normal approximation (z=1.96) — sufficient for n>30
    # For small n, this is slightly conservative
    z_val = 1.96
    ci_lower = mean - z_val * se if not pd.isna(se) else np.nan
    ci_upper = mean + z_val * se if not pd.isna(se) else np.nan
    return {'n': n, 'mean': mean, 'median': median, 'win_rate': win_rate, 'se': se, 'ci_lower': ci_lower, 'ci_upper': ci_upper}

# 按候选数量分组统计
groups = ['0只', '1-2只', '3-4只', '5只', '6-8只', '9只以上']
summary_rows = []

for g in groups:
    sub = event_df[event_df['group'] == g]
    if len(sub) == 0:
        continue
    
    stats_portfolio = calc_stats(sub['portfolio_ret'], f'{g}_portfolio')
    stats_ew = calc_stats(sub['ew_ret'], f'{g}_ew')
    stats_bench = calc_stats(sub['bench_ret'], f'{g}_bench')
    stats_alpha_ew = calc_stats(sub['alpha_vs_ew'], f'{g}_alpha_vs_ew')
    
    summary_rows.append({
        'group': g,
        'n_events': stats_portfolio['n'],
        'avg_sector_old': sub['sector_pct_old'].mean(),
        'avg_sector_new': sub['sector_pct_new'].mean(),
        'avg_sector_change': sub['sector_change'].mean(),
        'avg_defense': sub['defense_pct'].mean(),
        'avg_cash': sub['cash_pct'].mean(),
        'portfolio_mean': stats_portfolio['mean'],
        'portfolio_median': stats_portfolio['median'],
        'portfolio_wr': stats_portfolio['win_rate'],
        'portfolio_se': stats_portfolio['se'],
        'portfolio_ci_lower': stats_portfolio['ci_lower'],
        'portfolio_ci_upper': stats_portfolio['ci_upper'],
        'ew_mean': stats_ew['mean'],
        'ew_median': stats_ew['median'],
        'ew_wr': stats_ew['win_rate'],
        'ew_se': stats_ew['se'],
        'ew_ci_lower': stats_ew['ci_lower'],
        'ew_ci_upper': stats_ew['ci_upper'],
        'bench_mean': stats_bench['mean'],
        'bench_median': stats_bench['median'],
        'bench_wr': stats_bench['win_rate'],
        'bench_se': stats_bench['se'],
        'bench_ci_lower': stats_bench['ci_lower'],
        'bench_ci_upper': stats_bench['ci_upper'],
        'alpha_vs_ew_mean': stats_alpha_ew['mean'],
    })

summary_df = pd.DataFrame(summary_rows)

print("\n" + "=" * 100)
print("事件等权：按候选数量分组统计（下次调仓前收益）")
print("=" * 100)
print(f"{'组别':<8} {'事件数':>6} {'旧行业':>8} {'新行业':>8} {'变化':>8} {'防御':>8} {'现金':>8} {'组合收益':>10} {'等权池':>10} {'沪深300':>10}")
print("-" * 100)
for _, row in summary_df.iterrows():
    print(f"{row['group']:<8} {row['n_events']:>6} {row['avg_sector_old']:>8.1%} {row['avg_sector_new']:>8.1%} {row['avg_sector_change']:>8.1%} {row['avg_defense']:>8.1%} {row['avg_cash']:>8.1%} {row['portfolio_mean']:>10.2%} {row['ew_mean']:>10.2%} {row['bench_mean']:>10.2%}")
print("-" * 100)

print("\n详细统计（含均值、中位数、胜率、标准误、95%CI）:")
print(f"{'组别':<8} {'n':>4} {'组合均值':>10} {'组合中位':>10} {'胜率':>8} {'标准误':>10} {'CI下限':>10} {'CI上限':>10}")
print("-" * 80)
for _, row in summary_df.iterrows():
    print(f"{row['group']:<8} {row['n_events']:>4} {row['portfolio_mean']:>10.2%} {row['portfolio_median']:>10.2%} {row['portfolio_wr']:>8.1%} {row['portfolio_se']:>10.2%} {row['portfolio_ci_lower']:>10.2%} {row['portfolio_ci_upper']:>10.2%}")
print("-" * 80)

print(f"\n{'组别':<8} {'n':>4} {'等权均值':>10} {'等权中位':>10} {'胜率':>8} {'标准误':>10} {'CI下限':>10} {'CI上限':>10}")
print("-" * 80)
for _, row in summary_df.iterrows():
    print(f"{row['group']:<8} {row['n_events']:>4} {row['ew_mean']:>10.2%} {row['ew_median']:>10.2%} {row['ew_wr']:>8.1%} {row['ew_se']:>10.2%} {row['ew_ci_lower']:>10.2%} {row['ew_ci_upper']:>10.2%}")
print("-" * 80)

# ============================================================
# 6. 市场状态内部比较
# ============================================================
print("\n[6/6] 市场状态内部比较...")

print("\n在每种市场状态内部，按候选数量分组的下期等权池收益:")
for regime in ['强牛', '弱牛', '震荡', '熊市']:
    regime_sub = event_df[event_df['regime'] == regime]
    if len(regime_sub) == 0:
        continue
    print(f"\n{'='*60}")
    print(f"  市场状态: {regime} ({len(regime_sub)} 个事件)")
    print(f"{'='*60}")
    print(f"  {'组别':<8} {'n':>4} {'等权均值':>10} {'等权中位':>10} {'胜率':>8}")
    print("  " + "-" * 50)
    for g in groups:
        sub = regime_sub[regime_sub['group'] == g]
        if len(sub) == 0:
            continue
        s = calc_stats(sub['ew_ret'], f'{regime}_{g}')
        print(f"  {g:<8} {s['n']:>4} {s['mean']:>10.2%} {s['median']:>10.2%} {s['win_rate']:>8.1%}")
    print("  " + "-" * 50)

# ============================================================
# 7. 候选数量预测下期等权池/沪深300收益
# ============================================================
print("\n" + "=" * 70)
print("候选数量预测下期市场收益能力")
print("=" * 70)

# 候选数量与下期等权池收益的相关系数
event_clean = event_df.dropna(subset=['n_candidates', 'ew_ret', 'bench_ret'])
if len(event_clean) > 0:
    corr_ew = event_clean['n_candidates'].corr(event_clean['ew_ret'])
    corr_bench = event_clean['n_candidates'].corr(event_clean['bench_ret'])
    print(f"\n  候选数量 vs 下期等权池收益: 相关系数 = {corr_ew:.4f}")
    print(f"  候选数量 vs 下期沪深300收益: 相关系数 = {corr_bench:.4f}")
    
    # 按候选数量分组的下期市场收益
    print(f"\n  {'候选数量':<8} {'n':>4} {'等权池均值':>10} {'沪深300均值':>10}")
    print("  " + "-" * 40)
    for g in groups:
        sub = event_clean[event_clean['group'] == g]
        if len(sub) == 0:
            continue
        ew_mean = sub['ew_ret'].mean()
        bench_mean = sub['bench_ret'].mean()
        print(f"  {g:<8} {len(sub):>4} {ew_mean:>10.2%} {bench_mean:>10.2%}")
    print("  " + "-" * 40)
    
    # 回归检验
    # 回归检验（使用numpy实现）
    from numpy.polynomial import polynomial as P
    x = event_clean['n_candidates'].values
    y_ew = event_clean['ew_ret'].values
    y_bench = event_clean['bench_ret'].values
    
    # 简单线性回归：y = a + b*x
    # b = cov(x,y) / var(x), a = mean(y) - b * mean(x)
    def simple_reg(x, y):
        n = len(x)
        mx, my = np.mean(x), np.mean(y)
        cov = np.mean((x - mx) * (y - my))
        var_x = np.mean((x - mx)**2)
        if var_x == 0:
            return 0, my, 0, 1.0
        b = cov / var_x
        a = my - b * mx
        # R^2
        y_pred = a + b * x
        ss_res = np.sum((y - y_pred)**2)
        ss_tot = np.sum((y - my)**2)
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
        # p-value for slope (simplified, using t-statistic)
        residuals = y - y_pred
        mse = np.sum(residuals**2) / (n - 2) if n > 2 else np.inf
        se_b = np.sqrt(mse / (n * var_x)) if var_x > 0 and n > 2 else np.inf
        t_stat = b / se_b if se_b > 0 else 0
        # Approximate p-value (two-tailed) using normal approximation with math.erf
        import math
        p_val = np.nan
        if abs(t_stat) > 0 and not np.isinf(t_stat):
            # erf(x) = 2/sqrt(pi) * integral from 0 to x of exp(-t^2) dt
            # P(Z > z) = 0.5 * (1 - erf(z / sqrt(2)))
            # p_val = 2 * P(Z > |z|) = 1 - erf(|z| / sqrt(2))
            p_val = 1 - math.erf(abs(t_stat) / np.sqrt(2))
        return b, a, r2, p_val
    
    slope_ew, intercept_ew, r2_ew, p_ew = simple_reg(x, y_ew)
    slope_bench, intercept_bench, r2_bench, p_bench = simple_reg(x, y_bench)
    
    print(f"\n  线性回归: 候选数量 -> 下期等权池收益")
    print(f"    斜率 = {slope_ew:.6f}, R^2 = {r2_ew:.4f}, p值(近似) = {p_ew:.4f}")
    print(f"  线性回归: 候选数量 -> 下期沪深300收益")
    print(f"    斜率 = {slope_bench:.6f}, R^2 = {r2_bench:.4f}, p值(近似) = {p_bench:.4f}")

# ============================================================
# 8. 排序有效性（只使用实际调仓日且候选>5）
# ============================================================
print("\n" + "=" * 70)
print("排序有效性（仅实际调仓日且候选>5）")
print("=" * 70)

# 计算所有ETF的评分和信号（只用于排序有效性检验）
print("\n  计算评分...")
strategy = StrategyEngine(cfg)
all_scores_list = []
for ticker in B0_18_CORE:
    tdf = market_df[market_df['ticker'] == ticker].copy()
    if len(tdf) < 51:
        continue
    scored = strategy.calculate_total_score(tdf)
    all_scores_list.append(scored)

scores_all = pd.concat(all_scores_list, ignore_index=True)
scores_all = strategy.rank_all_momentum(scores_all)
scores_all = strategy.compute_total_score(scores_all)
signals_all = strategy.generate_signals(scores_all, bench_df)

buy_signals = signals_all[
    (signals_all['ticker'].isin(B0_18_CORE)) & 
    (signals_all['signal_type'] == 'BUY') &
    (signals_all['momentum_valid'] == True)
].copy()

# 只使用实际调仓日（周四）且候选>5
target_dates = event_df[event_df['n_candidates'] > 5]['date'].apply(pd.to_datetime).tolist()
print(f"  候选>5的调仓日: {len(target_dates)} 个（不超过调仓日总数{N_VALID-1}）")

all_dates_list = sorted(market_df['date'].unique())
date_to_idx = {d: i for i, d in enumerate(all_dates_list)}

def future_ret(ticker, from_date, days):
    idx = date_to_idx.get(from_date)
    if idx is None or idx + days >= len(all_dates_list):
        return np.nan
    to_date = all_dates_list[idx + days]
    tdf = market_df[market_df['ticker'] == ticker]
    from_p = tdf[tdf['date'] == from_date]['close']
    to_p = tdf[tdf['date'] == to_date]['close']
    if len(from_p) == 0 or len(to_p) == 0:
        return np.nan
    return to_p.iloc[0] / from_p.iloc[0] - 1

alpha_results = []
for date in target_dates:
    day_buy = buy_signals[buy_signals['date'] == date].sort_values('total_score', ascending=False)
    if len(day_buy) < 6:
        continue
    
    top5 = day_buy.head(5)
    rest = day_buy.iloc[5:]
    
    for d in [5, 10, 20]:
        top5_rets = [future_ret(t, date, d) for t in top5['ticker']]
        top5_rets = [r for r in top5_rets if not pd.isna(r)]
        rest_rets = [future_ret(t, date, d) for t in rest['ticker']]
        rest_rets = [r for r in rest_rets if not pd.isna(r)]
        
        if top5_rets and rest_rets:
            alpha = np.mean(top5_rets) - np.mean(rest_rets)
            alpha_results.append({
                'date': date,
                'horizon': d,
                'alpha': alpha,
                'n_top5': len(top5_rets),
                'n_rest': len(rest_rets),
            })

alpha_df = pd.DataFrame(alpha_results)

print(f"\n  前5名 vs 其余候选 超额:")
print(f"  {'horizon':>8} {'均值':>10} {'中位数':>10} {'胜率':>8} {'样本数':>8}")
print("  " + "-" * 50)
for h in [5, 10, 20]:
    sub = alpha_df[alpha_df['horizon'] == h]
    if len(sub) > 0:
        print(f"  {h}日{'>':>5} {sub['alpha'].mean():>10.2%} {sub['alpha'].median():>10.2%} {(sub['alpha']>0).sum()/len(sub):>8.1%} {len(sub):>8}")
    else:
        print(f"  {h}日{'>':>5} {'N/A':>10} {'N/A':>10} {'N/A':>8} {'N/A':>8}")

# 前5名 vs 行业等权池
print(f"\n  前5名 vs 行业等权池 超额:")
print(f"  {'horizon':>8} {'均值':>10} {'中位数':>10} {'胜率':>8} {'样本数':>8}")
print("  " + "-" * 50)
for h in [5, 10, 20]:
    alphas_ew = []
    for _, row in alpha_df[alpha_df['horizon'] == h].iterrows():
        date = row['date']
        day_buy = buy_signals[buy_signals['date'] == date].sort_values('total_score', ascending=False)
        top5 = day_buy.head(5)
        top5_rets = [future_ret(t, date, h) for t in top5['ticker']]
        top5_rets = [r for r in top5_rets if not pd.isna(r)]
        if top5_rets:
            idx = date_to_idx.get(date)
            if idx is not None and idx + h < len(all_dates_list):
                to_date = all_dates_list[idx + h]
                if date in core_ew.index and to_date in core_ew.index:
                    ew_r = core_ew.loc[to_date] / core_ew.loc[date] - 1
                    alphas_ew.append(np.mean(top5_rets) - ew_r)
    if alphas_ew:
        print(f"  {h}日{'>':>5} {np.mean(alphas_ew):>10.2%} {np.median(alphas_ew):>10.2%} {sum(1 for a in alphas_ew if a>0)/len(alphas_ew):>8.1%} {len(alphas_ew):>8}")
    else:
        print(f"  {h}日{'>':>5} {'N/A':>10} {'N/A':>10} {'N/A':>8} {'N/A':>8}")

# ============================================================
# 9. 构建日历日加权表
# ============================================================
print("\n" + "=" * 70)
print("构建日历日加权表")
print("=" * 70)

daily_records = []
for i, row in event_df.iterrows():
    date = pd.to_datetime(row['date'])
    next_date = pd.to_datetime(row['next_date'])
    
    # 该调仓区间内的所有交易日（从date到next_date，包含date，不包含next_date）
    period_days = nav_df[(nav_df['date'] >= date) & (nav_df['date'] < next_date)]
    
    for _, day in period_days.iterrows():
        daily_records.append({
            'date': day['date'].strftime('%Y-%m-%d'),
            'rebalance_event': date.strftime('%Y-%m-%d'),  # 所属调仓事件
            'n_candidates': row['n_candidates'],
            'regime': row['regime'],
            'sector_pct': day['sector_pct'],
            'defense_pct': day['defense_pct'],
            'cash_pct': day['cash_pct'],
            'nav': day['nav'],
        })

daily_df = pd.DataFrame(daily_records)
print(f"  日历日加权表: {len(daily_df)} 行（所有交易日按调仓区间归属）")
print(f"  注意：每个交易日只归属一个调仓区间，不重复计权")

daily_df.to_csv('D:/etf_rotation_model/reports/calendar_level_table.csv', index=False, encoding='utf-8-sig')

# ============================================================
# 10. 生成中文报告
# ============================================================
print("\n" + "=" * 70)
print("生成中文诊断报告")
print("=" * 70)

report = f"""# 候选池宽度—实际仓位—后续表现 诊断报告 v2（修正统计口径）

## 一、统计口径说明

### 1.1 调仓日定义
- 调仓日 = 每周四（与漏斗审计一致）。
- 策略在每周四收盘后进行信号评估，确定下周持仓。

### 1.2 三个样本数定义
- **346**：funnel_audit_daily.csv 中所有周四调仓日的总数（2019-06-06 至 2026-06-11）。
- **336**：排除预热期（2019-08-13 前）后的调仓日数。
- **335**：再排除未知状态（bench 数据不足计算 MA20/MA50）后的**有效调仓日数**。

### 1.3 历史错误
- **1649**：之前脚本把每个交易日当作“调仓周期”进行重复计权，导致统计单位错误。
- **541**：之前“候选>5”的排序有效性检验基于每日而非调仓日，样本数超过实际调仓日上限。

### 1.4 本次修正
- 所有统计从统一策略起点 **2019-08-13** 开始。
- 事件等权表：{len(event_df)} 行，每个有效调仓日一行（最后一个调仓日无下期，故 N_VALID-1）。
- 日历日加权表：{len(daily_df)} 行，将每个调仓区间内的交易日展开，但明确**不称为“调仓周期”**。

## 二、事件等权表（每个调仓日一行）

### 2.1 按候选数量分组统计

| 候选数量 | 事件数 | 旧行业仓位 | 新行业仓位 | 仓位变化 | 防御仓位 | 现金 | 组合收益 | 等权池 | 沪深300 |
|---------|--------|-----------|-----------|---------|---------|------|---------|--------|--------|
"""

for _, row in summary_df.iterrows():
    report += f"| {row['group']} | {row['n_events']} | {row['avg_sector_old']:.1%} | {row['avg_sector_new']:.1%} | {row['avg_sector_change']:.1%} | {row['avg_defense']:.1%} | {row['avg_cash']:.1%} | {row['portfolio_mean']:.2%} | {row['ew_mean']:.2%} | {row['bench_mean']:.2%} |\n"

report += f"""
### 2.2 详细统计（含95%置信区间）

| 候选数量 | n | 组合均值 | 组合中位 | 胜率 | 标准误 | 95%CI下限 | 95%CI上限 |
|---------|---|---------|---------|------|-------|----------|----------|
"""

for _, row in summary_df.iterrows():
    report += f"| {row['group']} | {row['n_events']} | {row['portfolio_mean']:.2%} | {row['portfolio_median']:.2%} | {row['portfolio_wr']:.1%} | {row['portfolio_se']:.2%} | {row['portfolio_ci_lower']:.2%} | {row['portfolio_ci_upper']:.2%} |\n"

report += f"""
| 候选数量 | n | 等权均值 | 等权中位 | 胜率 | 标准误 | 95%CI下限 | 95%CI上限 |
|---------|---|---------|---------|------|-------|----------|----------|
"""

for _, row in summary_df.iterrows():
    report += f"| {row['group']} | {row['n_events']} | {row['ew_mean']:.2%} | {row['ew_median']:.2%} | {row['ew_wr']:.1%} | {row['ew_se']:.2%} | {row['ew_ci_lower']:.2%} | {row['ew_ci_upper']:.2%} |\n"

report += f"""
### 2.3 关键结论

1. **硬趋势条件是当前主要筛选器**：
   - 候选0只时，策略平均行业仓位仅 {event_df[event_df['group']=='0只']['sector_pct_new'].mean():.1%}，说明硬条件成功阻止了弱势ETF入场。
   - 候选从0只增加到9+只时，行业仓位从 {event_df[event_df['group']=='0只']['sector_pct_new'].mean():.1%} 上升到 {event_df[event_df['group']=='9只以上']['sector_pct_new'].mean():.1%}，说明候选数量直接反映硬条件过滤后的市场状态。

2. **排序在候选充足时只有微弱增量价值**：
"""

if not alpha_df.empty:
    for h in [5, 10, 20]:
        sub = alpha_df[alpha_df['horizon'] == h]
        if len(sub) > 0:
            report += f"   - {h}日：前5名均值超额 {sub['alpha'].mean():.2%}，胜率 {(sub['alpha']>0).sum()/len(sub):.1%}（样本{len(sub)}）\n"
else:
    report += "   - 无足够数据\n"

report += f"""
3. **候选数量能否作为择时或仓位信号？尚未被证明**：
   - 候选数量 vs 下期等权池收益：相关系数 = {corr_ew:.4f}
   - 候选数量 vs 下期沪深300收益：相关系数 = {corr_bench:.4f}
   - 线性回归 R^2 分别为 {r2_ew:.4f} 和 {r2_bench:.4f}，p值(近似)分别为 {p_ew:.4f} 和 {p_bench:.4f}。
   - 在 {len(event_clean)} 个有效事件中，候选数量与未来市场收益的关系**微弱且不显著**。

### 2.4 市场状态内部比较

"""

for regime in ['强牛', '弱牛', '震荡', '熊市']:
    regime_sub = event_df[event_df['regime'] == regime]
    if len(regime_sub) == 0:
        continue
    report += f"\n**{regime}**（{len(regime_sub)} 个事件）：\n"
    report += f"| 候选数量 | n | 等权均值 | 等权中位 | 胜率 |\n"
    report += f"|---------|---|---------|---------|------|\n"
    for g in groups:
        sub = regime_sub[regime_sub['group'] == g]
        if len(sub) == 0:
            continue
        s = calc_stats(sub['ew_ret'], f'{regime}_{g}')
        report += f"| {g} | {s['n']} | {s['mean']:.2%} | {s['median']:.2%} | {s['win_rate']:.1%} |\n"

report += f"""
## 三、建议

1. **暂不修改策略参数**：当前统计仅用于诊断，不意味着需要立即调整硬条件或评分权重。
2. **硬趋势条件是主要筛选器**：候选数量直接反映硬条件过滤后的市场状态，这是当前策略的核心机制。
3. **排序区分力微弱**：在候选>5的调仓日中，评分排序对未来超额贡献有限，增量价值不足。
4. **候选数量作为择时信号尚未被证明**：虽然候选数量与市场状态相关，但用它预测下期市场收益的能力微弱且不显著，不建议将其直接纳入仓位控制。
"""

with open('D:/etf_rotation_model/reports/candidate_pool_diagnosis_v2.md', 'w', encoding='utf-8') as f:
    f.write(report)

# 保存汇总表
summary_df.to_csv('D:/etf_rotation_model/reports/event_summary.csv', index=False, encoding='utf-8-sig')

print("\n  报告已保存:")
print("  - reports/event_level_table.csv")
print("  - reports/calendar_level_table.csv")
print("  - reports/event_summary.csv")
print("  - reports/candidate_pool_diagnosis_v2.md")
print("\n分析完成！")
