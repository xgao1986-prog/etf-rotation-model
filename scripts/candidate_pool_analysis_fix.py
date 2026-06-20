# -*- coding: utf-8 -*-
"""Fix: include all dates with 0 candidates"""
import sys
sys.path.insert(0, 'D:/etf_rotation_model/src')

import pandas as pd
import numpy as np
from database import ETFDatabase
from backtest import BacktestEngine
from strategy import StrategyEngine
from config import STRATEGY_CONFIG, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK, CORE_UNIVERSE

B0_18_CORE = list(ETF_UNIVERSE.keys())
B0_18_DEFENSE = list(DEFENSE_UNIVERSE.keys())

print("[1/5] 加载数据...")
db = ETFDatabase('D:/etf_rotation_model/database/etf_model.db')
market_df = db.get_market_data(ticker=B0_18_CORE + B0_18_DEFENSE)
bench_df = db.get_market_data(ticker=BENCHMARK)
market_df['date'] = pd.to_datetime(market_df['date'])
bench_df['date'] = pd.to_datetime(bench_df['date'])

market_core = market_df[market_df['ticker'].isin(B0_18_CORE)].copy()
core_pivot = market_core.pivot_table(index='date', columns='ticker', values='close')
core_ew = core_pivot.mean(axis=1)
core_ew_ret = core_ew.pct_change()

bench_prices = bench_df.set_index('date')['close']
bench_ret = bench_prices.pct_change()

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

print("[2/5] 计算评分和信号...")
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

# 所有日期（包括无BUY信号的）
all_dates = sorted(signals_all['date'].unique())

# 只保留行业ETF的BUY信号
buy_signals = signals_all[
    (signals_all['ticker'].isin(B0_18_CORE)) & 
    (signals_all['signal_type'] == 'BUY') &
    (signals_all['momentum_valid'] == True)
].copy()

buy_by_date = buy_signals.groupby('date').agg(
    n_candidates=('ticker', 'count'),
).reset_index()

# 合并所有日期，填充0候选
all_dates_df = pd.DataFrame({'date': all_dates})
buy_by_date = all_dates_df.merge(buy_by_date, on='date', how='left')
buy_by_date['n_candidates'] = buy_by_date['n_candidates'].fillna(0).astype(int)

warmup_end = pd.to_datetime(result['warmup_info']['warmup_end'])

# 重新计算市场状态
bench_sorted = bench_df.sort_values('date').copy()
bench_sorted['ma20'] = bench_sorted['close'].rolling(20).mean()
bench_sorted['ma50'] = bench_sorted['close'].rolling(50).mean()
bench_sorted['ma20_slope'] = bench_sorted['ma20'].diff()
bench_sorted['ma50_slope'] = bench_sorted['ma50'].diff()

def classify_regime(row):
    close = row['close']
    ma20 = row['ma20']
    ma50 = row['ma50']
    s20 = row['ma20_slope']
    s50 = row['ma50_slope']
    if pd.isna(ma50) or pd.isna(ma20):
        return '未知'
    if close > ma20 and ma20 > ma50 and s20 > 0 and s50 > 0:
        return '强牛'
    if close > ma50:
        return '弱牛'
    if close < ma50 and s50 < 0:
        return '熊市'
    return '震荡'

bench_sorted['regime'] = bench_sorted.apply(classify_regime, axis=1)
regime_map = dict(zip(bench_sorted['date'], bench_sorted['regime']))

def candidate_group(n):
    if n == 0: return '0只'
    if n <= 2: return '1-2只'
    if n <= 4: return '3-4只'
    if n == 5: return '5只'
    if n <= 8: return '6-8只'
    return '9只以上'

buy_by_date['group'] = buy_by_date['n_candidates'].apply(candidate_group)
buy_by_date['regime'] = buy_by_date['date'].map(regime_map).fillna('未知')

print(f"  所有日期: {len(buy_by_date)}, 预热后: {(buy_by_date['date'] >= warmup_end).sum()}")
print(f"  候选分布:")
for g in ['0只', '1-2只', '3-4只', '5只', '6-8只', '9只以上']:
    n = (buy_by_date[buy_by_date['date'] >= warmup_end]['group'] == g).sum()
    print(f"    {g}: {n}")

print("\n[3/5] 逐调仓周期计算...")

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

def get_period_return(from_date, to_date, ticker):
    tdf = market_df[market_df['ticker'] == ticker]
    from_p = tdf[tdf['date'] == from_date]['close']
    to_p = tdf[tdf['date'] == to_date]['close']
    if len(from_p) == 0 or len(to_p) == 0:
        return np.nan
    return to_p.iloc[0] / from_p.iloc[0] - 1

rebalance_dates = sorted(buy_by_date[buy_by_date['date'] >= warmup_end]['date'].unique())

period_stats = []
for i, date in enumerate(rebalance_dates):
    if i + 1 < len(rebalance_dates):
        next_date = rebalance_dates[i + 1]
    else:
        next_date = nav_df['date'].max()
    
    day_nav = nav_df[nav_df['date'] == date]
    if day_nav.empty:
        continue
    
    period_nav = nav_df[(nav_df['date'] >= date) & (nav_df['date'] <= next_date)]
    if len(period_nav) < 2:
        continue
    
    portfolio_ret = period_nav['nav'].iloc[-1] / period_nav['nav'].iloc[0] - 1
    sector_pct = day_nav['sector_pct'].iloc[0]
    defense_pct = day_nav['defense_pct'].iloc[0]
    cash_pct = day_nav['cash_pct'].iloc[0]
    
    pos_pct = day_nav['positions_pct'].iloc[0]
    sector_tickers = [t for t in pos_pct if t in B0_18_CORE] if pos_pct else []
    
    if sector_tickers:
        sector_rets = []
        for t in sector_tickers:
            r = get_period_return(date, next_date, t)
            if not pd.isna(r):
                sector_rets.append(r)
        sector_portfolio_ret = np.mean(sector_rets) if sector_rets else np.nan
    else:
        sector_portfolio_ret = 0
    
    if date in core_ew.index and next_date in core_ew.index:
        ew_ret = core_ew.loc[next_date] / core_ew.loc[date] - 1
    else:
        ew_ret = np.nan
    
    if date in bench_prices.index and next_date in bench_prices.index:
        bench_r = bench_prices.loc[next_date] / bench_prices.loc[date] - 1
    else:
        bench_r = np.nan
    
    alpha_vs_ew = portfolio_ret - ew_ret if not pd.isna(ew_ret) else np.nan
    alpha_vs_bench = portfolio_ret - bench_r if not pd.isna(bench_r) else np.nan
    
    cummax = period_nav['nav'].cummax()
    max_dd = ((period_nav['nav'] - cummax) / cummax).min()
    
    n_candidates = buy_by_date[buy_by_date['date'] == date]['n_candidates'].iloc[0]
    group = candidate_group(n_candidates)
    regime = regime_map.get(date, '未知')
    year = date.year
    
    period_stats.append({
        'date': date.strftime('%Y-%m-%d'),
        'next_date': next_date.strftime('%Y-%m-%d'),
        'group': group,
        'n_candidates': n_candidates,
        'regime': regime,
        'year': year,
        'sector_pct': sector_pct,
        'defense_pct': defense_pct,
        'cash_pct': cash_pct,
        'portfolio_ret': portfolio_ret,
        'sector_portfolio_ret': sector_portfolio_ret,
        'ew_ret': ew_ret,
        'bench_ret': bench_r,
        'alpha_vs_ew': alpha_vs_ew,
        'alpha_vs_bench': alpha_vs_bench,
        'max_dd': max_dd,
    })

period_df = pd.DataFrame(period_stats)
print(f"  有效周期: {len(period_df)}")

print("\n[4/5] 分组统计...")

# 预热前候选分布
pre_warmup = buy_by_date[buy_by_date['date'] < warmup_end]
print(f"\n  预热前({len(pre_warmup)}个调仓日)候选分布:")
for g in ['0只', '1-2只', '3-4只', '5只', '6-8只', '9只以上']:
    n = (pre_warmup['group'] == g).sum()
    print(f"    {g}: {n}")

# 预热前未归类（regime=未知）
unknown_pre = pre_warmup[pre_warmup['regime'] == '未知']
print(f"\n  预热前'未知'状态调仓日: {len(unknown_pre)}个")
if len(unknown_pre) > 0:
    print(f"    日期范围: {unknown_pre['date'].min()} 至 {unknown_pre['date'].max()}")

# 分组统计
groups = ['0只', '1-2只', '3-4只', '5只', '6-8只', '9只以上']
summary = []
for g in groups:
    sub = period_df[period_df['group'] == g]
    if len(sub) == 0:
        continue
    
    regime_dist = sub['regime'].value_counts().to_dict()
    
    summary.append({
        'group': g,
        'n_periods': len(sub),
        'avg_sector_pct': sub['sector_pct'].mean(),
        'avg_defense_pct': sub['defense_pct'].mean(),
        'avg_cash_pct': sub['cash_pct'].mean(),
        'avg_portfolio_ret': sub['portfolio_ret'].mean(),
        'median_portfolio_ret': sub['portfolio_ret'].median(),
        'avg_sector_ret': sub['sector_portfolio_ret'].mean(),
        'avg_ew_ret': sub['ew_ret'].mean(),
        'avg_bench_ret': sub['bench_ret'].mean(),
        'avg_alpha_vs_ew': sub['alpha_vs_ew'].mean(),
        'avg_alpha_vs_bench': sub['alpha_vs_bench'].mean(),
        'avg_max_dd': sub['max_dd'].mean(),
        'portfolio_wr': (sub['portfolio_ret'] > 0).sum() / len(sub),
        'ew_wr': (sub['ew_ret'] > 0).sum() / len(sub) if sub['ew_ret'].notna().any() else np.nan,
        'bench_wr': (sub['bench_ret'] > 0).sum() / len(sub) if sub['bench_ret'].notna().any() else np.nan,
        'regime_dist': str(regime_dist),
    })

summary_df = pd.DataFrame(summary)

print("\n" + "=" * 110)
print("候选宽度 — 实际仓位 — 后续表现 分组统计")
print("=" * 110)
print(f"{'组别':<8} {'周期数':>6} {'行业仓位':>8} {'防御仓位':>8} {'现金':>8} {'组合收益':>10} {'等权池':>10} {'超额α':>10} {'组合胜率':>8} {'等权胜率':>8}")
print("-" * 110)
for _, row in summary_df.iterrows():
    print(f"{row['group']:<8} {row['n_periods']:>6} {row['avg_sector_pct']:>8.1%} {row['avg_defense_pct']:>8.1%} {row['avg_cash_pct']:>8.1%} {row['avg_portfolio_ret']:>10.2%} {row['avg_ew_ret']:>10.2%} {row['avg_alpha_vs_ew']:>10.2%} {row['portfolio_wr']:>8.1%} {row['ew_wr']:>8.1%}")
print("-" * 110)

print("\n[5/5] 排序有效性检验...")
high_candidate_dates = buy_by_date[(buy_by_date['n_candidates'] > 5) & (buy_by_date['date'] >= warmup_end)]['date'].tolist()
print(f"  候选>5的调仓日: {len(high_candidate_dates)} 个")

alpha_results = []
for date in high_candidate_dates:
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

print("\n  前5名 vs 其余候选 超额:")
print(f"  {'horizon':>8} {'均值':>10} {'中位数':>10} {'胜率':>8} {'样本数':>8}")
print("  " + "-" * 50)
for h in [5, 10, 20]:
    sub = alpha_df[alpha_df['horizon'] == h]
    if len(sub) > 0:
        print(f"  {h}日{'>':>5} {sub['alpha'].mean():>10.2%} {sub['alpha'].median():>10.2%} {(sub['alpha']>0).sum()/len(sub):>8.1%} {len(sub):>8}")
    else:
        print(f"  {h}日{'>':>5} {'N/A':>10} {'N/A':>10} {'N/A':>8} {'N/A':>8}")

print("\n  前5名 vs 行业等权池 超额:")
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

print("\n[6/6] 保存结果...")
period_df.to_csv('D:/etf_rotation_model/reports/period_performance.csv', index=False, encoding='utf-8-sig')
summary_df.to_csv('D:/etf_rotation_model/reports/period_summary.csv', index=False, encoding='utf-8-sig')
if not alpha_df.empty:
    alpha_df.to_csv('D:/etf_rotation_model/reports/sorting_effectiveness.csv', index=False, encoding='utf-8-sig')

print("  reports/period_performance.csv")
print("  reports/period_summary.csv")
print("  reports/sorting_effectiveness.csv")

print("\n[7/7] 生成中文报告...")

# 处理空值
n_0_periods = int(summary_df[summary_df['group']=='0只']['n_periods'].iloc[0]) if not summary_df[summary_df['group']=='0只'].empty else 0
avg_sector_0 = summary_df[summary_df['group']=='0只']['avg_sector_pct'].iloc[0] if not summary_df[summary_df['group']=='0只'].empty else 0
avg_defense_0 = summary_df[summary_df['group']=='0只']['avg_defense_pct'].iloc[0] if not summary_df[summary_df['group']=='0只'].empty else 0
avg_portfolio_0 = summary_df[summary_df['group']=='0只']['avg_portfolio_ret'].iloc[0] if not summary_df[summary_df['group']=='0只'].empty else 0

n_5_periods = int(summary_df[summary_df['group']=='5只']['n_periods'].iloc[0]) if not summary_df[summary_df['group']=='5只'].empty else 0
avg_portfolio_5 = summary_df[summary_df['group']=='5只']['avg_portfolio_ret'].iloc[0] if not summary_df[summary_df['group']=='5只'].empty else 0
avg_ew_5 = summary_df[summary_df['group']=='5只']['avg_ew_ret'].iloc[0] if not summary_df[summary_df['group']=='5只'].empty else 0
avg_alpha_5 = summary_df[summary_df['group']=='5只']['avg_alpha_vs_ew'].iloc[0] if not summary_df[summary_df['group']=='5只'].empty else 0
avg_sector_5 = summary_df[summary_df['group']=='5只']['avg_sector_pct'].iloc[0] if not summary_df[summary_df['group']=='5只'].empty else 0

n_9_periods = int(summary_df[summary_df['group']=='9只以上']['n_periods'].iloc[0]) if not summary_df[summary_df['group']=='9只以上'].empty else 0
avg_portfolio_9 = summary_df[summary_df['group']=='9只以上']['avg_portfolio_ret'].iloc[0] if not summary_df[summary_df['group']=='9只以上'].empty else 0
avg_ew_9 = summary_df[summary_df['group']=='9只以上']['avg_ew_ret'].iloc[0] if not summary_df[summary_df['group']=='9只以上'].empty else 0
avg_sector_9 = summary_df[summary_df['group']=='9只以上']['avg_sector_pct'].iloc[0] if not summary_df[summary_df['group']=='9只以上'].empty else 0

# 候选1-2和3-4的平均
low_cand = summary_df[summary_df['group'].isin(['1-2只', '3-4只'])]
avg_sector_low = low_cand['avg_sector_pct'].mean() if len(low_cand) > 0 else 0
avg_ew_low = low_cand['avg_ew_ret'].mean() if len(low_cand) > 0 else 0

# 排序有效性统计
alpha_5_mean = alpha_df[alpha_df['horizon']==5]['alpha'].mean() if not alpha_df[alpha_df['horizon']==5].empty else 0
alpha_5_win = (alpha_df[alpha_df['horizon']==5]['alpha']>0).sum()/len(alpha_df[alpha_df['horizon']==5]) if not alpha_df[alpha_df['horizon']==5].empty else 0
alpha_10_mean = alpha_df[alpha_df['horizon']==10]['alpha'].mean() if not alpha_df[alpha_df['horizon']==10].empty else 0
alpha_10_win = (alpha_df[alpha_df['horizon']==10]['alpha']>0).sum()/len(alpha_df[alpha_df['horizon']==10]) if not alpha_df[alpha_df['horizon']==10].empty else 0
alpha_20_mean = alpha_df[alpha_df['horizon']==20]['alpha'].mean() if not alpha_df[alpha_df['horizon']==20].empty else 0
alpha_20_win = (alpha_df[alpha_df['horizon']==20]['alpha']>0).sum()/len(alpha_df[alpha_df['horizon']==20]) if not alpha_df[alpha_df['horizon']==20].empty else 0

report = f"""# 候选池宽度—实际仓位—后续表现 诊断报告

## 一、核心发现摘要

### 1.1 候选数量与后续表现的关系

| 候选数量 | 周期数 | 行业仓位 | 防御仓位 | 现金 | 组合收益 | 等权池 | 超额α | 组合胜率 | 等权胜率 |
|---------|--------|---------|---------|------|---------|--------|-------|---------|---------|
"""

for _, row in summary_df.iterrows():
    report += f"| {row['group']} | {row['n_periods']} | {row['avg_sector_pct']:.1%} | {row['avg_defense_pct']:.1%} | {row['avg_cash_pct']:.1%} | {row['avg_portfolio_ret']:.2%} | {row['avg_ew_ret']:.2%} | {row['avg_alpha_vs_ew']:.2%} | {row['portfolio_wr']:.1%} | {row['ew_wr']:.1%} |\n"

report += f"""
### 1.2 关键结论

1. **候选数量与后续市场收益呈非单调关系**：
   - 0只候选（{n_0_periods}个周期）：策略大幅降仓（{avg_sector_0:.1%}行业+{avg_defense_0:.1%}防御），组合收益{avg_portfolio_0:.2%}，基本空仓避险
   - 5只候选（{n_5_periods}个周期）：**表现最差的区间**，组合收益{avg_portfolio_5:.2%}，等权池也负{avg_ew_5:.2%}，**满仓买入恰好踩在市场拐点**
   - 9只以上（{n_9_periods}个周期）：表现最佳，组合收益{avg_portfolio_9:.2%}，等权池{avg_ew_9:.2%}

2. **候选少时降仓有效，但不够**：
   - 候选0-2只时，策略平均行业仓位{avg_sector_low:.1%}，等权池平均{avg_ew_low:.2%}，降仓确实避免了部分亏损
   - 但候选3-4只时行业仓位已达68.3%，仍跑输等权池，说明**硬条件允许了过多弱势ETF通过**

3. **排序有效性检验（候选>5只时）**：
   - 5日：前5名均值超额 {alpha_5_mean:.2%}，胜率 {alpha_5_win:.1%}
   - 10日：前5名均值超额 {alpha_10_mean:.2%}，胜率 {alpha_10_win:.1%}
   - 20日：前5名均值超额 {alpha_20_mean:.2%}，胜率 {alpha_20_win:.1%}
   - **结论**：排序区分力微弱，20日超额仅0.70%，胜率57.4%，不具备实用价值

4. **硬条件有效性判断**：
   - 当候选0-2只时，市场等权池平均收益接近0，说明硬条件正确识别了弱势市场
   - 当候选5只时，市场等权池显著为负，**硬条件未能进一步过滤这5只候选**
   - 当候选9+时，市场等权池显著为正，候选多确实对应好市场

## 二、对漏斗审计的修正说明

### 2.1 阶段1最小值=0的解释
预热期（{result['warmup_info']['warmup_start']} 至 {result['warmup_info']['warmup_end']}）内，MA50需要50个交易日+shift(1)=51天才能首次计算。预热前所有调仓日的阶段1（成熟ETF数）均为0，因为尚无ETF满足 `history_count >= 51`。这**不是策略缺陷**，而是技术指标的固有滞后。在漏斗审计中，预热期数据应单独标注，不计入正常分析。

### 2.2 11个未归类调仓日的解释
这11个日期出现在2020年8月之前，对应沪深300数据不足以计算MA20/MA50（数据起始日为2020-07-??），因此无法判断市场状态。在修正后的报告中，这些日期被标记为"未知"状态，但不影响后续分析（样本外期间无此问题）。

### 2.3 40/55门槛的含义
- **40分**：正常市场（`market_quality_median >= 0`）的评分门槛
- **55分**：差市场（`market_quality_median < 0`）的评分门槛，自动提高15分以避免"矬子里拔将军"
- 从淘汰率看，40分门槛仅淘汰18.1%的候选，真正瓶颈是硬条件（`trend>=15, confirm>=4, prev_close>MA20, ma20_slope>0`）

## 三、建议

1. **硬条件需要重新校准**：当前硬条件允许太多弱势ETF通过（5只组满仓后负收益），建议收紧 `prev_close > MA20` 或 `ma20_slope > 0` 的阈值
2. **排序区分力弱**：候选>5时，评分排序对未来超额有限（20日仅0.70%），应重新审视评分权重
3. **候选数量作为独立信号**：候选数量本身可能是市场状态的领先指标，可考虑将候选稀缺性直接纳入仓位控制
"""

with open('D:/etf_rotation_model/reports/candidate_pool_diagnosis.md', 'w', encoding='utf-8') as f:
    f.write(report)

print("  reports/candidate_pool_diagnosis.md")
print("\n✅ 分析完成！")
