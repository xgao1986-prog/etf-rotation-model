
# -*- coding: utf-8 -*-
"""
B0-18 候选池宽度与筛选漏斗审计脚本

目标：逐调仓日统计策略筛选漏斗各阶段数量，回答：
1. 策略是"排名主导"还是"门槛主导"？
2. 当前40分门槛是否只是名义门槛？
3. 不同门槛（35/45/50）下候选数量如何变化？

漏斗阶段（按策略执行顺序）：
  阶段0: 16只行业ETF（core universe）
  阶段1: 数据成熟可交易（history_count >= 51 AND momentum_valid）
  阶段2: 通过硬条件（trend>=15, confirm>=4, prev_close>ma20, ma20_slope>0）
  阶段3: 总分达到门槛（好市场>=40, 差市场>=55，或测试35/45/50）
  阶段4: 相关性处理（软惩罚+硬去重）—— 注：B0-18中未触发，本审计用阶段3近似
  阶段5: 实际BUY信号（signal_type==BUY）

注意：横截面动量排名在阶段1（成熟可交易ETF）中计算，但总分排序在阶段3后进行。
"""

import sys, os
sys.path.insert(0, 'D:/etf_rotation_model/src')

import pandas as pd
import numpy as np
from datetime import datetime

from database import ETFDatabase
from strategy import StrategyEngine
from config import STRATEGY_CONFIG, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK

B0_18_CORE = list(ETF_UNIVERSE.keys())
B0_18_ALL = B0_18_CORE + list(DEFENSE_UNIVERSE.keys())

# ============================================================
# 1. 加载数据并计算策略信号
# ============================================================
print("[1/3] 加载数据并计算策略信号...")
db = ETFDatabase('D:/etf_rotation_model/database/etf_model.db')
market_df = db.get_market_data(ticker=B0_18_ALL)
bench_df = db.get_market_data(ticker=BENCHMARK)

market_df['date'] = pd.to_datetime(market_df['date'])
bench_df['date'] = pd.to_datetime(bench_df['date'])

cfg = STRATEGY_CONFIG.copy()
strategy = StrategyEngine(cfg)

# 计算所有core ETF的指标
all_scores = []
for ticker in B0_18_CORE:
    tdf = market_df[market_df['ticker'] == ticker].copy()
    if len(tdf) < 51:
        continue
    scored = strategy.calculate_total_score(tdf)
    all_scores.append(scored)

scores_all = pd.concat(all_scores, ignore_index=True)
scores_all = strategy.rank_all_momentum(scores_all)
scores_all = strategy.compute_total_score(scores_all)  # 计算total_score

# 获取调仓日（周四）
all_dates = sorted(scores_all['date'].unique())
rebalance_dates = [d for d in all_dates if pd.to_datetime(d).weekday() == 3]  # 周四=3

print(f"  总交易日: {len(all_dates)}, 调仓日: {len(rebalance_dates)}")

# ============================================================
# 2. 逐调仓日统计漏斗
# ============================================================
print("[2/3] 逐调仓日统计漏斗...")

def market_regime(date):
    """简单市场状态：强牛/弱牛/震荡/熊市"""
    bdf = bench_df[bench_df['date'] == date]
    if bdf.empty:
        return '未知'
    row = bdf.iloc[0]
    # 需要计算ma20和ma50
    bench_recent = bench_df[bench_df['date'] <= date].tail(60)
    if len(bench_recent) < 50:
        return '未知'
    bench_recent = bench_recent.sort_values('date').reset_index(drop=True)
    bench_recent['ma20'] = bench_recent['close'].rolling(20).mean()
    bench_recent['ma50'] = bench_recent['close'].rolling(50).mean()
    bench_recent['ma20_slope'] = bench_recent['ma20'].diff()
    bench_recent['ma50_slope'] = bench_recent['ma50'].diff()
    latest = bench_recent.iloc[-1]
    close = latest['close']
    ma20 = latest['ma20']
    ma50 = latest['ma50']
    s20 = latest['ma20_slope']
    s50 = latest['ma50_slope']
    if pd.isna(ma50):
        return '未知'
    if close > ma20 and ma20 > ma50 and s20 > 0 and s50 > 0:
        return '强牛'
    if close > ma50:
        return '弱牛'
    if close < ma50 and s50 < 0:
        return '熊市'
    return '震荡'

# 对每个调仓日，计算漏斗各阶段
funnel_records = []

for date in rebalance_dates:
    day_sigs = scores_all[scores_all['date'] == date].copy()
    if day_sigs.empty:
        continue
    
    # 阶段0: 16只行业ETF
    stage0 = len(B0_18_CORE)
    
    # 阶段1: 数据成熟可交易（history_count >= 51 AND momentum_valid）
    core_day = day_sigs[day_sigs['ticker'].isin(B0_18_CORE)]
    mature = core_day[(core_day['history_count'] >= 51) & (core_day['momentum_valid'] == True)]
    stage1 = len(mature)
    
    # 阶段2: 通过硬条件（trend>=15, confirm>=4, prev_close>ma20, ma20_slope>0）
    # 需要prev_close（前一日收盘价）——从完整数据中查找，不在当前行shift
    prev_close_map = {}
    for ticker in mature['ticker'].unique():
        ticker_df = scores_all[(scores_all['ticker'] == ticker) & (scores_all['date'] < date)].sort_values('date')
        if not ticker_df.empty:
            prev_close_map[ticker] = ticker_df['close'].iloc[-1]
    mature = mature.copy()
    mature['prev_close'] = mature['ticker'].map(prev_close_map)
    hard_pass = mature[
        (mature['trend_score'] >= cfg['min_trend_score']) &
        (mature['confirm_score'] >= cfg['min_confirm_score']) &
        (mature['prev_close'] > mature['ma20']) &
        (mature['ma20_slope'] > 0)
    ]
    stage2 = len(hard_pass)
    
    # 计算市场质量（momentum_20中位数）
    market_median = core_day['momentum_20'].median()
    is_poor_market = market_median < 0 if not pd.isna(market_median) else False
    
    # 阶段3: 总分达到门槛（当前=40好/55差，以及测试35/45/50）
    def stage3_count(df, threshold_good, threshold_poor):
        eff_threshold = threshold_poor if is_poor_market else threshold_good
        return len(df[df['total_score'] >= eff_threshold])
    
    stage3_40 = stage3_count(hard_pass, 40, 55)
    stage3_35 = stage3_count(hard_pass, 35, 50)  # 差市场也降5
    stage3_45 = stage3_count(hard_pass, 45, 60)
    stage3_50 = stage3_count(hard_pass, 50, 65)
    
    # 阶段5: 实际BUY信号（使用当前策略的generate_signals）
    # 需要先生成完整的signals，然后只取当日
    # 用简单逻辑模拟BUY信号（与generate_signals一致）
    day_all_signals = scores_all[scores_all['date'] == date].copy()
    if len(day_all_signals) > 0:
        # 获取前一日收盘价
        pc_map = {}
        for t in day_all_signals['ticker'].unique():
            tdf = scores_all[(scores_all['ticker'] == t) & (scores_all['date'] < date)].sort_values('date')
            if not tdf.empty:
                pc_map[t] = tdf['close'].iloc[-1]
        day_all_signals['prev_close'] = day_all_signals['ticker'].map(pc_map)
        # 核心池入场条件（简化版，不考虑market_timing和bull_market）
        _core = day_all_signals[day_all_signals['ticker'].isin(B0_18_CORE)]
        _mature = _core[(_core['history_count'] >= 51) & (_core['momentum_valid'] == True)]
        _buy = _mature[
            (_mature['trend_score'] >= cfg['min_trend_score']) &
            (_mature['confirm_score'] >= cfg['min_confirm_score']) &
            (_mature['total_score'] >= (55 if is_poor_market else 40)) &
            (_mature['prev_close'] > _mature['ma20']) &
            (_mature['ma20_slope'] > 0)
        ]
        stage5 = len(_buy)
    else:
        stage5 = 0
    
    # 记录每只合格ETF的分数和排名
    qualified_scores = []
    if len(_buy) > 0:
        _buy_sorted = _buy.sort_values('total_score', ascending=False).reset_index(drop=True)
        for idx, row in _buy_sorted.iterrows():
            qualified_scores.append({
                'ticker': row['ticker'],
                'total_score': row['total_score'],
                'rank': idx + 1,
                'trend_score': row['trend_score'],
                'confirm_score': row['confirm_score'],
                'momentum_rank': row['momentum_rank'],
                'volume_score': row['volume_score'],
                'vol_score': row['vol_score'],
            })
    
    funnel_records.append({
        'date': date,
        'year': pd.to_datetime(date).year,
        'regime': market_regime(date),
        'is_poor_market': is_poor_market,
        'market_median_momentum': market_median,
        
        'stage0_total_core': stage0,
        'stage1_mature': stage1,
        'stage2_hard_pass': stage2,
        'stage3_35': stage3_35,
        'stage3_40': stage3_40,
        'stage3_45': stage3_45,
        'stage3_50': stage3_50,
        'stage5_buy_signals': stage5,
        
        'qualified_scores': qualified_scores,
    })

funnel_df = pd.DataFrame(funnel_records)
print(f"  漏斗记录: {len(funnel_df)} 条调仓日")

# ============================================================
# 3. 生成汇总统计
# ============================================================
print("[3/3] 生成汇总统计和报告...")

lines = []
lines.append("# B0-18 候选池宽度与筛选漏斗审计报告")
lines.append("")
lines.append(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
lines.append(f"统计区间: {funnel_df['date'].min()} ~ {funnel_df['date'].max()}")
lines.append(f"调仓日数量: {len(funnel_df)}")
lines.append("")

# --- 3.1 先筛选后排序，还是先排序后筛选？ ---
lines.append("## 一、当前策略执行顺序：先排名，后筛选")
lines.append("")
lines.append("代码执行顺序：")
lines.append("1. `calculate_indicators` → 计算各因子（trend/confirm/momentum/volume/vol）")
lines.append("2. `rank_all_momentum` → 在**全部成熟行业ETF**中做横截面动量排名（strategy.py:137）")
lines.append("3. `compute_total_score` → 汇总总评分（strategy.py:194）")
lines.append("4. `generate_signals` → **筛选**：trend>=15, confirm>=4, total_score>=40, 价>MA20, 斜率>0（strategy.py:398）")
lines.append("5. 相关性去重/软惩罚 → 在B0-18中未触发")
lines.append("")
lines.append("**结论：当前策略是『先排名后筛选』**。所有成熟可交易的行业ETF都参与动量排名，")
lines.append("但只有同时通过硬条件+总分门槛的ETF才会被标记为BUY。")
lines.append("这意味着：如果阶段1（成熟可交易）有10只，但阶段3（总分达标）只有3只，")
lines.append("那排名实际上只在3只中起作用，而不是在10只中竞争。")
lines.append("")

# --- 3.2 漏斗各阶段汇总统计 ---
lines.append("## 二、漏斗各阶段汇总统计（每调仓日）")
lines.append("")

for col, label in [
    ('stage0_total_core', '阶段0: 16只行业ETF'),
    ('stage1_mature', '阶段1: 数据成熟可交易'),
    ('stage2_hard_pass', '阶段2: 通过硬条件'),
    ('stage3_40', '阶段3: 总分>=40/55'),
    ('stage5_buy_signals', '阶段5: 实际BUY信号'),
]:
    s = funnel_df[col]
    lines.append(f"### {label}")
    lines.append(f"- 平均: {s.mean():.1f}")
    lines.append(f"- 中位数: {s.median():.1f}")
    lines.append(f"- 最小: {s.min():.0f}")
    lines.append(f"- 25%分位: {s.quantile(0.25):.1f}")
    lines.append(f"- 75%分位: {s.quantile(0.75):.1f}")
    lines.append(f"- 90%分位: {s.quantile(0.90):.1f}")
    lines.append("")

# --- 3.3 候选数量分布 ---
lines.append("## 三、候选数量分布（阶段3: 总分>=40/55）")
lines.append("")

def count_distribution(s, name):
    lines.append(f"### {name}")
    total = len(s)
    bins = [
        (0, 0, '0只'),
        (1, 2, '1-2只'),
        (3, 4, '3-4只'),
        (5, 5, '刚好5只'),
        (6, 8, '6-8只'),
        (9, 999, '9只以上'),
    ]
    for lo, hi, label in bins:
        count = ((s >= lo) & (s <= hi)).sum()
        pct = count / total * 100
        lines.append(f"- {label}: {count} 天 ({pct:.1f}%)")
    lines.append("")

count_distribution(funnel_df['stage3_40'], '当前门槛(40/55)')
count_distribution(funnel_df['stage3_35'], '测试门槛35/50')
count_distribution(funnel_df['stage3_45'], '测试门槛45/60')
count_distribution(funnel_df['stage3_50'], '测试门槛50/65')

# --- 3.4 不足5只/刚好5只/超过5只 ---
lines.append("## 四、『不足5只』『刚好5只』『超过5只』分布")
lines.append("")
for col, name in [('stage3_40', '当前门槛40/55'), ('stage3_45', '测试门槛45/60')]:
    s = funnel_df[col]
    total = len(s)
    less5 = (s < 5).sum()
    exact5 = (s == 5).sum()
    more5 = (s > 5).sum()
    lines.append(f"**{name}**:")
    lines.append(f"- 不足5只: {less5} 天 ({less5/total*100:.1f}%)")
    lines.append(f"- 刚好5只: {exact5} 天 ({exact5/total*100:.1f}%)")
    lines.append(f"- 超过5只: {more5} 天 ({more5/total*100:.1f}%)")
    lines.append("")

# --- 3.5 按年份拆分 ---
lines.append("## 五、按年份拆分（阶段3: 总分>=40/55）")
lines.append("")
lines.append("| 年份 | 调仓日数 | 平均成熟 | 平均硬通过 | 平均达标 | 平均BUY | 不足5只占比 | 超过5只占比 |")
lines.append("|------|----------|----------|------------|----------|---------|-------------|-------------|")
for year in sorted(funnel_df['year'].unique()):
    sub = funnel_df[funnel_df['year'] == year]
    n = len(sub)
    avg_mature = sub['stage1_mature'].mean()
    avg_hard = sub['stage2_hard_pass'].mean()
    avg_40 = sub['stage3_40'].mean()
    avg_buy = sub['stage5_buy_signals'].mean()
    pct_less5 = (sub['stage3_40'] < 5).sum() / n * 100
    pct_more5 = (sub['stage3_40'] > 5).sum() / n * 100
    lines.append(f"| {year} | {n} | {avg_mature:.1f} | {avg_hard:.1f} | {avg_40:.1f} | {avg_buy:.1f} | {pct_less5:.1f}% | {pct_more5:.1f}% |")
lines.append("")

# --- 3.6 按市场状态拆分 ---
lines.append("## 六、按市场状态拆分（阶段3: 总分>=40/55）")
lines.append("")
lines.append("| 状态 | 调仓日数 | 平均成熟 | 平均硬通过 | 平均达标 | 平均BUY | 差市场占比 | 不足5只占比 |")
lines.append("|------|----------|----------|------------|----------|---------|------------|-------------|")
for regime in ['强牛', '弱牛', '震荡', '熊市']:
    sub = funnel_df[funnel_df['regime'] == regime]
    if len(sub) == 0:
        continue
    n = len(sub)
    avg_mature = sub['stage1_mature'].mean()
    avg_hard = sub['stage2_hard_pass'].mean()
    avg_40 = sub['stage3_40'].mean()
    avg_buy = sub['stage5_buy_signals'].mean()
    pct_poor = sub['is_poor_market'].sum() / n * 100
    pct_less5 = (sub['stage3_40'] < 5).sum() / n * 100
    lines.append(f"| {regime} | {n} | {avg_mature:.1f} | {avg_hard:.1f} | {avg_40:.1f} | {avg_buy:.1f} | {pct_poor:.1f}% | {pct_less5:.1f}% |")
lines.append("")

# --- 3.7 关键结论 ---
lines.append("## 七、关键结论")
lines.append("")

# 计算几个关键比例
s3_40 = funnel_df['stage3_40']
s3_45 = funnel_df['stage3_45']
stage2 = funnel_df['stage2_hard_pass']

lines.append(f"1. **硬条件 vs 总分门槛的筛选力度**：")
lines.append(f"   - 通过硬条件（阶段2）后，平均有 {stage2.mean():.1f} 只候选")
lines.append(f"   - 当前门槛40/55后，平均有 {s3_40.mean():.1f} 只候选")
lines.append(f"   - 门槛进一步过滤掉 {(stage2 - s3_40).mean():.1f} 只（平均）")
lines.append(f"   - 门槛淘汰率: {((stage2 - s3_40) / stage2.replace(0, np.nan)).mean():.1%}")
lines.append("")

lines.append(f"2. **当前门槛是否只是名义门槛？**：")
pct_10plus = (s3_40 >= 10).sum() / len(s3_40) * 100
pct_5plus = (s3_40 >= 5).sum() / len(s3_40) * 100
lines.append(f"   - 达标>=10只的调仓日: {pct_10plus:.1f}%")
lines.append(f"   - 达标>=5只的调仓日: {pct_5plus:.1f}%")
lines.append(f"   - 如果绝大多数日期都有>=5只达标，策略主要靠排名选前5；")
lines.append(f"   - 如果经常不足5只，策略在『凑满仓』与『保持空缺』之间摇摆。")
lines.append("")

lines.append(f"3. **门槛提高的影响**：")
lines.append(f"   - 门槛35/50: 平均 {funnel_df['stage3_35'].mean():.1f} 只达标")
lines.append(f"   - 门槛40/55: 平均 {funnel_df['stage3_40'].mean():.1f} 只达标（当前）")
lines.append(f"   - 门槛45/60: 平均 {funnel_df['stage3_45'].mean():.1f} 只达标")
lines.append(f"   - 门槛50/65: 平均 {funnel_df['stage3_50'].mean():.1f} 只达标")
lines.append("")

# 保存报告
report_path = 'D:/etf_rotation_model/reports/funnel_audit.md'
with open(report_path, 'w', encoding='utf-8') as f:
    f.write('\n'.join(lines))

print(f"\n报告已保存: {report_path}")

# 保存逐日CSV
csv_records = []
for _, row in funnel_df.iterrows():
    csv_records.append({
        'date': row['date'].strftime('%Y-%m-%d'),
        'year': row['year'],
        'regime': row['regime'],
        'is_poor_market': row['is_poor_market'],
        'market_median_momentum': f"{row['market_median_momentum']:.2%}" if not pd.isna(row['market_median_momentum']) else 'N/A',
        'stage0_total_core': row['stage0_total_core'],
        'stage1_mature': row['stage1_mature'],
        'stage2_hard_pass': row['stage2_hard_pass'],
        'stage3_35': row['stage3_35'],
        'stage3_40': row['stage3_40'],
        'stage3_45': row['stage3_45'],
        'stage3_50': row['stage3_50'],
        'stage5_buy_signals': row['stage5_buy_signals'],
        'loss_from_hard_to_score': row['stage2_hard_pass'] - row['stage3_40'],
        'loss_from_score_to_buy': row['stage3_40'] - row['stage5_buy_signals'],
    })

csv_df = pd.DataFrame(csv_records)
csv_path = 'D:/etf_rotation_model/reports/funnel_audit_daily.csv'
csv_df.to_csv(csv_path, index=False, encoding='utf-8-sig')

print(f"逐日CSV已保存: {csv_path}")
print(f"  行数: {len(csv_df)}")

# 输出关键统计摘要
print("\n" + "="*60)
print("漏斗审计摘要")
print("="*60)
print(f"调仓日总数: {len(funnel_df)}")
print(f"\n阶段2（硬条件通过）: 平均={stage2.mean():.1f}, 中位数={stage2.median():.1f}")
print(f"阶段3-40（当前门槛）: 平均={s3_40.mean():.1f}, 中位数={s3_40.median():.1f}")
print(f"阶段3-45（测试门槛）: 平均={s3_45.mean():.1f}, 中位数={s3_45.median():.1f}")
print(f"\n不足5只的调仓日: {(s3_40 < 5).sum()}/{len(s3_40)} = {(s3_40 < 5).sum()/len(s3_40)*100:.1f}%")
print(f"刚好5只的调仓日: {(s3_40 == 5).sum()}/{len(s3_40)} = {(s3_40 == 5).sum()/len(s3_40)*100:.1f}%")
print(f"超过5只的调仓日: {(s3_40 > 5).sum()}/{len(s3_40)} = {(s3_40 > 5).sum()/len(s3_40)*100:.1f}%")
print(f"\n>=10只达标的调仓日: {(s3_40 >= 10).sum()}/{len(s3_40)} = {(s3_40 >= 10).sum()/len(s3_40)*100:.1f}%")
print(f">=5只达标的调仓日: {(s3_40 >= 5).sum()}/{len(s3_40)} = {(s3_40 >= 5).sum()/len(s3_40)*100:.1f}%")
print("="*60)
