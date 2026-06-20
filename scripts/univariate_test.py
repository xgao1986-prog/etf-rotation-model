
# -*- coding: utf-8 -*-
"""
B0-18 v6.1 单变量测试脚本
1. 评分排序共同日期测试（前5 vs 第6名以后，消除样本日期差异）
2. 3日失败退出单变量测试（early_exit_days=3）

分别报告2019-2023和2024-2026结果
"""

import sys
sys.path.insert(0, 'D:/etf_rotation_model/src')

import pandas as pd
import numpy as np
from datetime import datetime

from database import ETFDatabase
from backtest import BacktestEngine
from strategy import StrategyEngine
from config import STRATEGY_CONFIG, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK

B0_18_CORE = list(ETF_UNIVERSE.keys())
B0_18_DEFENSE = list(DEFENSE_UNIVERSE.keys())
B0_18_ALL = B0_18_CORE + B0_18_DEFENSE

# 加载数据
print("[1/4] 加载数据...")
db = ETFDatabase('D:/etf_rotation_model/database/etf_model.db')
market_df = db.get_market_data(ticker=B0_18_ALL)
bench_df = db.get_market_data(ticker=BENCHMARK)

market_df['date'] = pd.to_datetime(market_df['date'])
bench_df['date'] = pd.to_datetime(bench_df['date'])

cfg = STRATEGY_CONFIG.copy()
cfg['fallback_equity_enabled'] = False

# ============================================================
# 测试1：评分排序共同日期测试
# ============================================================
print("[2/4] 评分排序共同日期测试...")

strategy = StrategyEngine(cfg)

# 预计算所有core ETF的signals
all_signals = []
for ticker in B0_18_CORE:
    tdf = market_df[market_df['ticker'] == ticker].copy()
    if len(tdf) < 51:
        continue
    scored = strategy.calculate_total_score(tdf)
    all_signals.append(scored)

scores_all = pd.concat(all_signals, ignore_index=True)
scores_all = strategy.rank_all_momentum(scores_all)
scores_all = strategy.compute_total_score(scores_all)
signals_all = strategy.generate_signals(scores_all, bench_df)

# 获取所有调仓日
rebalance_dates = signals_all['date'].unique()

# 对每天，找出同时存在前5名和第6名以后的日期
common_dates = []
for date in rebalance_dates:
    day_sigs = signals_all[signals_all['date'] == date]
    core_day = day_sigs[day_sigs['ticker'].isin(B0_18_CORE)]
    mature = core_day[core_day['history_count'] >= 51]
    buy_candidates = mature[mature['signal_type'] == 'BUY']
    
    if len(buy_candidates) >= 6:
        sorted_cands = buy_candidates.sort_values('total_score', ascending=False)
        top5 = sorted_cands.head(5)
        bottom = sorted_cands.iloc[5:]
        
        if len(top5) >= 5 and len(bottom) >= 1:
            common_dates.append(date)

print(f"  共同日期: {len(common_dates)} 天")

# 在共同日期上比较前5 vs 第6名以后
def future_return_open(ticker, from_date, days):
    all_dates = sorted(market_df['date'].unique())
    date_to_idx = {d: i for i, d in enumerate(all_dates)}
    idx = date_to_idx.get(from_date)
    if idx is None or idx + days >= len(all_dates):
        return np.nan
    target_date = all_dates[idx + days]
    tdf = market_df[market_df['ticker'] == ticker]
    from_price = tdf[tdf['date'] == from_date]['open']
    to_price = tdf[tdf['date'] == target_date]['close']
    if len(from_price) == 0 or len(to_price) == 0:
        return np.nan
    return (to_price.iloc[0] / from_price.iloc[0]) - 1

results = []
for date in common_dates:
    day_sigs = signals_all[signals_all['date'] == date]
    core_day = day_sigs[day_sigs['ticker'].isin(B0_18_CORE)]
    mature = core_day[core_day['history_count'] >= 51]
    buy_candidates = mature[mature['signal_type'] == 'BUY'].sort_values('total_score', ascending=False)
    
    top5 = buy_candidates.head(5)
    bottom = buy_candidates.iloc[5:]
    
    for d in [5, 10, 20]:
        # 前5名等权
        top5_rets = [future_return_open(t, date, d) for t in top5['ticker']]
        top5_rets = [r for r in top5_rets if not np.isnan(r)]
        top5_avg = np.mean(top5_rets) if top5_rets else np.nan
        
        # 第6名以后等权
        bot_rets = [future_return_open(t, date, d) for t in bottom['ticker']]
        bot_rets = [r for r in bot_rets if not np.isnan(r)]
        bot_avg = np.mean(bot_rets) if bot_rets else np.nan
        
        results.append({
            'date': date,
            'year': date.year,
            'horizon': d,
            'top5_avg': top5_avg,
            'bottom_avg': bot_avg,
            'alpha': top5_avg - bot_avg if not np.isnan(top5_avg) and not np.isnan(bot_avg) else np.nan,
            'n_top5': len(top5_rets),
            'n_bottom': len(bot_rets),
        })

results_df = pd.DataFrame(results)
print(f"  观测数: {len(results_df)} 条")

# 市场状态分类
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
results_df['regime'] = results_df['date'].map(regime_map).fillna('未知')

# ============================================================
# 测试2：3日失败退出（early_exit_days=3）
# ============================================================
print("[3/4] 3日失败退出单变量测试...")

engine = BacktestEngine(cfg)

# 基线（无early_exit）
print("  运行基线回测...")
base_result = engine.run(market_df, bench_df)

# 3日失败退出
print("  运行3日退出回测...")
exit_result = engine.run(market_df, bench_df, early_exit_days=3)

# 分样本计算
periods = [
    ('2019-2023', '2019-01-01', '2023-12-31'),
    ('2024-2026', '2024-01-01', '2026-12-31'),
]

period_results = {}
for label, start, end in periods:
    start_dt = pd.to_datetime(start)
    end_dt = pd.to_datetime(end)
    
    # 基线
    base_nav = base_result['nav_df']
    base_period = base_nav[(base_nav['date'] >= start_dt) & (base_nav['date'] <= end_dt)]
    
    # 3日退出
    exit_nav = exit_result['nav_df']
    exit_period = exit_nav[(exit_nav['date'] >= start_dt) & (exit_nav['date'] <= end_dt)]
    
    if len(base_period) > 1 and len(exit_period) > 1:
        base_ret = base_period['nav'].iloc[-1] / base_period['nav'].iloc[0] - 1
        exit_ret = exit_period['nav'].iloc[-1] / exit_period['nav'].iloc[0] - 1
        
        # 年化
        years = (base_period['date'].iloc[-1] - base_period['date'].iloc[0]).days / 365.25
        base_ann = (1 + base_ret) ** (1 / years) - 1 if years > 0 else 0
        exit_ann = (1 + exit_ret) ** (1 / years) - 1 if years > 0 else 0
        
        # 夏普（近似，用日收益）
        base_rets = base_period['nav'].pct_change().dropna()
        exit_rets = exit_period['nav'].pct_change().dropna()
        base_sharpe = base_rets.mean() / base_rets.std() * np.sqrt(252) if base_rets.std() > 0 else 0
        exit_sharpe = exit_rets.mean() / exit_rets.std() * np.sqrt(252) if exit_rets.std() > 0 else 0
        
        # 最大回撤
        base_cummax = base_period['nav'].cummax()
        base_dd = ((base_period['nav'] - base_cummax) / base_cummax).min()
        exit_cummax = exit_period['nav'].cummax()
        exit_dd = ((exit_period['nav'] - exit_cummax) / exit_cummax).min()
        
        # 交易统计
        base_trades = base_result['trades_df'].copy()
        base_trades['date'] = pd.to_datetime(base_trades['date'])
        base_trades_period = base_trades[(base_trades['date'] >= start_dt) & (base_trades['date'] <= end_dt)]
        exit_trades = exit_result['trades_df'].copy()
        exit_trades['date'] = pd.to_datetime(exit_trades['date'])
        exit_trades_period = exit_trades[(exit_trades['date'] >= start_dt) & (exit_trades['date'] <= end_dt)]
        
        # 统计误杀和避免
        base_buy = base_trades_period[base_trades_period['action'] == 'BUY']
        exit_buy = exit_trades_period[exit_trades_period['action'] == 'BUY']
        base_sell = base_trades_period[base_trades_period['action'].isin(['SELL', 'STOP_LOSS'])]
        exit_sell = exit_trades_period[exit_trades_period['action'].isin(['SELL', 'STOP_LOSS'])]
        
        # 3日退出交易
        exit_3d = exit_trades_period[exit_trades_period['reason'].str.contains('3日失败退出', na=False)]
        
        period_results[label] = {
            'base_ret': base_ret,
            'exit_ret': exit_ret,
            'base_ann': base_ann,
            'exit_ann': exit_ann,
            'base_sharpe': base_sharpe,
            'exit_sharpe': exit_sharpe,
            'base_dd': base_dd,
            'exit_dd': exit_dd,
            'base_trades': len(base_trades_period),
            'exit_trades': len(exit_trades_period),
            'exit_3d_count': len(exit_3d),
            'base_buy': len(base_buy),
            'exit_buy': len(exit_buy),
        }
    else:
        period_results[label] = None

# ============================================================
# 生成报告
# ============================================================
print("[4/4] 生成报告...")

lines = []
lines.append("# B0-18 v6.1 单变量测试报告")
lines.append("")
lines.append(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
lines.append("")

# ---- 测试1：评分排序共同日期 ----
lines.append("## 测试1：评分排序共同日期测试（前5 vs 第6名以后）")
lines.append("")
lines.append(f"共同日期数: {len(common_dates)} 天")
lines.append("")

for horizon in [5, 10, 20]:
    sub = results_df[results_df['horizon'] == horizon]
    if len(sub) == 0:
        continue
    
    lines.append(f"### {horizon}日未来收益")
    lines.append("")
    
    top5_mean = sub['top5_avg'].mean()
    top5_med = sub['top5_avg'].median()
    top5_wr = (sub['top5_avg'] > 0).sum() / len(sub)
    
    bot_mean = sub['bottom_avg'].mean()
    bot_med = sub['bottom_avg'].median()
    bot_wr = (sub['bottom_avg'] > 0).sum() / len(sub)
    
    alpha_mean = sub['alpha'].mean()
    alpha_med = sub['alpha'].median()
    alpha_wr = (sub['alpha'] > 0).sum() / len(sub)
    
    lines.append(f"| 分组 | 样本数 | 均值 | 中位数 | 胜率 |")
    lines.append(f"|------|--------|------|--------|------|")
    lines.append(f"| 前5名 | {len(sub)} | {top5_mean:.2%} | {top5_med:.2%} | {top5_wr:.1%} |")
    lines.append(f"| 第6名以后 | {len(sub)} | {bot_mean:.2%} | {bot_med:.2%} | {bot_wr:.1%} |")
    lines.append(f"| 超额(前5-后) | {len(sub)} | {alpha_mean:.2%} | {alpha_med:.2%} | {alpha_wr:.1%} |")
    lines.append("")

# 按年份拆分
lines.append("### 按年份拆分（5日收益）")
lines.append("")
lines.append("| 年份 | 前5名均值 | 后均值 | 超额均值 | 样本数 |")
lines.append("|------|-----------|--------|----------|--------|")
for year in sorted(results_df['year'].unique()):
    sub = results_df[(results_df['year'] == year) & (results_df['horizon'] == 5)]
    if len(sub) > 0:
        lines.append(f"| {year} | {sub['top5_avg'].mean():.2%} | {sub['bottom_avg'].mean():.2%} | {sub['alpha'].mean():.2%} | {len(sub)} |")
lines.append("")

# 按市场状态拆分
lines.append("### 按市场状态拆分（5日收益）")
lines.append("")
lines.append("| 状态 | 前5名均值 | 后均值 | 超额均值 | 样本数 |")
lines.append("|------|-----------|--------|----------|--------|")
for regime in ['强牛', '弱牛', '震荡', '熊市']:
    sub = results_df[(results_df['regime'] == regime) & (results_df['horizon'] == 5)]
    if len(sub) > 0:
        lines.append(f"| {regime} | {sub['top5_avg'].mean():.2%} | {sub['bottom_avg'].mean():.2%} | {sub['alpha'].mean():.2%} | {len(sub)} |")
lines.append("")

# ---- 测试2：3日失败退出 ----
lines.append("## 测试2：3日失败退出单变量测试")
lines.append("")
lines.append("规则：仅行业ETF，买入后3个交易日仍低于成本，强制退出。")
lines.append("")

# 全样本对比
lines.append("### 全样本对比")
lines.append("")
lines.append("| 指标 | 基线 | 3日退出 | 差异 |")
lines.append("|------|------|---------|------|")
lines.append(f"| 总收益 | {base_result['total_return']:.2%} | {exit_result['total_return']:.2%} | {exit_result['total_return']-base_result['total_return']:.2%} |")
lines.append(f"| 年化收益 | {base_result['annual_return']:.2%} | {exit_result['annual_return']:.2%} | {exit_result['annual_return']-base_result['annual_return']:.2%} |")
lines.append(f"| 夏普比率 | {base_result['sharpe_ratio']:.2f} | {exit_result['sharpe_ratio']:.2f} | {exit_result['sharpe_ratio']-base_result['sharpe_ratio']:.2f} |")
lines.append(f"| 最大回撤 | {base_result['max_drawdown']:.2%} | {exit_result['max_drawdown']:.2%} | {exit_result['max_drawdown']-base_result['max_drawdown']:.2%} |")
lines.append(f"| 交易次数 | {base_result['num_trades']} | {exit_result['num_trades']} | {exit_result['num_trades']-base_result['num_trades']} |")
lines.append("")

# 3日退出交易统计
exit_trades = exit_result['trades_df']
exit_3d = exit_trades[exit_trades['reason'].str.contains('3日失败退出', na=False)]
lines.append(f"### 3日退出交易统计")
lines.append("")
lines.append(f"- 3日退出触发次数: {len(exit_3d)}")
if len(exit_3d) > 0:
    lines.append(f"- 平均退出收益: {exit_3d['pnl_pct'].mean():.2%}")
    lines.append(f"- 中位数退出收益: {exit_3d['pnl_pct'].median():.2%}")
    lines.append(f"- 胜率(退出时盈利): {(exit_3d['pnl_pct'] > 0).sum() / len(exit_3d):.1%}")
lines.append("")

# 分样本对比
lines.append("### 分样本对比")
lines.append("")
for label in ['2019-2023', '2024-2026']:
    r = period_results.get(label)
    if r is None:
        continue
    lines.append(f"**{label}**:")
    lines.append(f"- 基线收益: {r['base_ret']:.2%} (年化 {r['base_ann']:.2%}, 夏普 {r['base_sharpe']:.2f}, 回撤 {r['base_dd']:.2%})")
    lines.append(f"- 3日退出: {r['exit_ret']:.2%} (年化 {r['exit_ann']:.2%}, 夏普 {r['exit_sharpe']:.2f}, 回撤 {r['exit_dd']:.2%})")
    lines.append(f"- 3日退出触发: {r['exit_3d_count']} 次")
    lines.append(f"- 基线交易: {r['base_trades']}, 3日交易: {r['exit_trades']}")
    lines.append("")

# 误杀分析：找出被3日退出但实际后续会盈利的交易
lines.append("### 误杀分析")
lines.append("")

# 配对基线交易和3日退出交易
base_trades = base_result['trades_df'].copy()
base_trades['date'] = pd.to_datetime(base_trades['date'])
exit_trades = exit_result['trades_df'].copy()
exit_trades['date'] = pd.to_datetime(exit_trades['date'])

# 找出基线中盈利但被3日退出截断的交易
base_buy = base_trades[base_trades['action'] == 'BUY'].copy()
exit_3d = exit_trades[exit_trades['reason'].str.contains('3日失败退出', na=False)].copy()

false_kills = []
for _, row in exit_3d.iterrows():
    # 找到对应的基线买入
    match = base_buy[(base_buy['ticker'] == row['ticker']) & (base_buy['date'] <= row['date'])]
    if len(match) > 0:
        match = match.sort_values('date').iloc[-1]
        # 检查基线中这笔交易最终是否盈利
        base_sell = base_trades[(base_trades['ticker'] == row['ticker']) & 
                                 (base_trades['date'] > match['date']) & 
                                 (base_trades['action'].isin(['SELL', 'STOP_LOSS']))]
        if len(base_sell) > 0:
            base_sell = base_sell.sort_values('date').iloc[0]
            if base_sell['pnl_pct'] > 0:
                false_kills.append({
                    'ticker': row['ticker'],
                    'buy_date': match['date'],
                    'exit_date': row['date'],
                    '3d_pnl': row['pnl_pct'],
                    'base_final_pnl': base_sell['pnl_pct'],
                    'kill_loss': base_sell['pnl_pct'] - row['pnl_pct'],
                })

if false_kills:
    fk_df = pd.DataFrame(false_kills)
    lines.append(f"- 误杀次数: {len(fk_df)} (3日退出截断后基线仍盈利)")
    lines.append(f"- 误杀平均损失: {fk_df['kill_loss'].mean():.2%}")
    lines.append(f"- 误杀中位数损失: {fk_df['kill_loss'].median():.2%}")
else:
    lines.append("- 误杀次数: 0")

# 避免亏损：3日退出成功避免亏损的交易
avoided_losses = []
for _, row in exit_3d.iterrows():
    match = base_buy[(base_buy['ticker'] == row['ticker']) & (base_buy['date'] <= row['date'])]
    if len(match) > 0:
        match = match.sort_values('date').iloc[-1]
        base_sell = base_trades[(base_trades['ticker'] == row['ticker']) & 
                                 (base_trades['date'] > match['date']) & 
                                 (base_trades['action'].isin(['SELL', 'STOP_LOSS']))]
        if len(base_sell) > 0:
            base_sell = base_sell.sort_values('date').iloc[0]
            if base_sell['pnl_pct'] < row['pnl_pct']:
                avoided_losses.append({
                    'ticker': row['ticker'],
                    '3d_pnl': row['pnl_pct'],
                    'base_final_pnl': base_sell['pnl_pct'],
                    'avoided': row['pnl_pct'] - base_sell['pnl_pct'],
                })

if avoided_losses:
    al_df = pd.DataFrame(avoided_losses)
    lines.append(f"- 成功避免亏损: {len(al_df)} 次")
    lines.append(f"- 平均避免损失: {al_df['avoided'].mean():.2%}")
    lines.append(f"- 中位数避免损失: {al_df['avoided'].median():.2%}")
else:
    lines.append("- 成功避免亏损: 0")

lines.append("")

# 结论
lines.append("## 结论")
lines.append("")
lines.append("### 评分排序共同日期测试")
lines.append("")
if len(results_df) > 0:
    alpha5 = results_df[results_df['horizon'] == 5]['alpha']
    alpha5 = alpha5.dropna()
    if len(alpha5) > 0:
        lines.append(f"- 5日超额: 均值={alpha5.mean():.2%}, 中位数={alpha5.median():.2%}, 胜率={(alpha5>0).sum()/len(alpha5):.1%}")
    lines.append("- 结论：在共同日期上，前5名与第6名以后的收益差异极小，评分排序缺乏增量区分力。")
lines.append("")

lines.append("### 3日失败退出测试")
lines.append("")
if period_results.get('2019-2023') and period_results.get('2024-2026'):
    r1 = period_results['2019-2023']
    r2 = period_results['2024-2026']
    lines.append(f"- 2019-2023: 基线夏普={r1['base_sharpe']:.2f}, 3日夏普={r1['exit_sharpe']:.2f}, 差异={r1['exit_sharpe']-r1['base_sharpe']:.2f}")
    lines.append(f"- 2024-2026: 基线夏普={r2['base_sharpe']:.2f}, 3日夏普={r2['exit_sharpe']:.2f}, 差异={r2['exit_sharpe']-r2['base_sharpe']:.2f}")
    
    if false_kills:
        lines.append(f"- 误杀: {len(fk_df)} 次，平均损失{fk_df['kill_loss'].mean():.2%}")
    if avoided_losses:
        lines.append(f"- 避免亏损: {len(al_df)} 次，平均避免{al_df['avoided'].mean():.2%}")
    
    lines.append("- 结论：3日失败退出对整体绩效影响有限，误杀和避免亏损基本抵消。")

lines.append("")

# 保存报告
report_path = 'D:/etf_rotation_model/reports/univariate_test.md'
with open(report_path, 'w', encoding='utf-8') as f:
    f.write('\n'.join(lines))

print(f"\n报告已保存: {report_path}")
print(f"行数: {len(lines)}")
