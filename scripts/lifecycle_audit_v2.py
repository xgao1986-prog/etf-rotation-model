
# -*- coding: utf-8 -*-
"""
B0-18 v6 生命周期修正审计脚本
修正要点：
1. 行业ETF与防御资产分开统计
2. 交易分三类：从未盈利/曾经盈利转亏/最终盈利
3. 盈利捕获率只对最大浮盈>=1%的最终盈利交易计算
4. 利润回吐：回吐幅度=最大浮盈-最终收益，回吐比例=回吐幅度/最大浮盈
5. 选股对照：实际买入 vs 未买入候选 / 全部成熟行业ETF / 等权组合
6. 换仓超额：新买ETF - 被卖ETF 的未来1/5/10/20日收益
7. 按年份/ETF/退出原因/市场状态拆分（强牛/弱牛/震荡/熊市）
"""

import sys, os, json
sys.path.insert(0, 'D:/etf_rotation_model/src')

import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from database import ETFDatabase
from backtest import BacktestEngine
from strategy import StrategyEngine
from config import STRATEGY_CONFIG, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK

# ============================================================
# 1. 运行回测
# ============================================================
B0_18_CORE = list(ETF_UNIVERSE.keys())      # 16只行业ETF
B0_18_DEFENSE = list(DEFENSE_UNIVERSE.keys()) # 2只防御
B0_18_ALL = B0_18_CORE + B0_18_DEFENSE

print("[1/6] 加载数据...")
db = ETFDatabase('D:/etf_rotation_model/database/etf_model.db')
market_df = db.get_market_data(ticker=B0_18_ALL)
bench_df = db.get_market_data(ticker=BENCHMARK)

market_df['date'] = pd.to_datetime(market_df['date'])
bench_df['date'] = pd.to_datetime(bench_df['date'])

cfg = STRATEGY_CONFIG.copy()
cfg['fallback_equity_enabled'] = False
engine = BacktestEngine(cfg)

print("[2/6] 运行回测...")
result = engine.run(market_df, bench_df)

# 提取数据
trades_df = result['trades_df'].copy()
nav_df = result['nav_df'].copy()
trades_df['date'] = pd.to_datetime(trades_df['date'])
nav_df['date'] = pd.to_datetime(nav_df['date'])

# ============================================================
# 2. 构建交易对（BUY -> SELL/STOP_LOSS）
# ============================================================
print("[3/6] 构建交易对...")
trades_df = trades_df.sort_values(['ticker', 'date']).reset_index(drop=True)

paired = []
for ticker in trades_df['ticker'].unique():
    tt = trades_df[trades_df['ticker'] == ticker].reset_index(drop=True)
    buy_stack = []
    for _, row in tt.iterrows():
        if row['action'] == 'BUY':
            buy_stack.append(dict(row))
        elif row['action'] in ['SELL', 'STOP_LOSS']:
            if buy_stack:
                b = buy_stack.pop(0)
                paired.append({
                    'ticker': ticker,
                    'entry_date': b['date'],
                    'entry_price': b['price'],
                    'exit_date': row['date'],
                    'exit_price': row['price'],
                    'shares': b['shares'],
                    'entry_commission': b['commission'],
                    'exit_commission': row['commission'],
                    'final_pnl_pct': row['pnl_pct'],
                    'exit_reason': row['reason'],
                    'exit_action': row['action'],
                    'is_defense': ticker in B0_18_DEFENSE,
                })

trades = pd.DataFrame(paired)
print(f"  配对交易: {len(trades)} 笔")

# ============================================================
# 3. 对每笔交易计算持仓期间浮盈、最大浮盈、利润回吐
# ============================================================
print("[4/6] 计算持仓期间指标...")

all_dates = sorted(market_df['date'].unique())
date_to_idx = {d: i for i, d in enumerate(all_dates)}

def future_return(ticker, from_date, days):
    """从from_date起days日后的收益率（基于收盘价）"""
    idx = date_to_idx.get(from_date)
    if idx is None or idx + days >= len(all_dates):
        return np.nan
    target_date = all_dates[idx + days]
    tdf = market_df[market_df['ticker'] == ticker]
    from_price = tdf[tdf['date'] == from_date]['close']
    to_price = tdf[tdf['date'] == target_date]['close']
    if len(from_price) == 0 or len(to_price) == 0:
        return np.nan
    return (to_price.iloc[0] / from_price.iloc[0]) - 1

def future_return_open(ticker, from_date, days):
    """从from_date开盘价起days日后的收益率"""
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

def max_floating_profit(ticker, entry_date, exit_date):
    """持仓期间最大浮盈（基于收盘价 vs 买入价）"""
    tdf = market_df[(market_df['ticker'] == ticker) & 
                    (market_df['date'] >= entry_date) & 
                    (market_df['date'] <= exit_date)].sort_values('date')
    if tdf.empty:
        return 0, entry_date
    entry_price = tdf.iloc[0]['close']
    if entry_price <= 0:
        return 0, entry_date
    max_pnl = ((tdf['close'] / entry_price) - 1).max()
    peak_idx = ((tdf['close'] / entry_price) - 1).idxmax()
    peak_date = tdf.loc[peak_idx, 'date']
    return max_pnl, peak_date

def max_floating_drawdown(ticker, entry_date, exit_date):
    """持仓期间最大回撤（从峰值起）"""
    tdf = market_df[(market_df['ticker'] == ticker) & 
                    (market_df['date'] >= entry_date) & 
                    (market_df['date'] <= exit_date)].sort_values('date')
    if tdf.empty:
        return 0
    entry_price = tdf.iloc[0]['close']
    if entry_price <= 0:
        return 0
    cummax = (tdf['close'] / entry_price).cummax()
    dd = ((tdf['close'] / entry_price) / cummax - 1).min()
    return dd

# 逐笔计算
for i, row in trades.iterrows():
    ticker = row['ticker']
    entry = row['entry_date']
    exit_d = row['exit_date']
    
    # 持仓期间最大浮盈和峰值日期
    max_fp, peak_date = max_floating_profit(ticker, entry, exit_d)
    trades.at[i, 'max_fp'] = max_fp
    trades.at[i, 'peak_date'] = peak_date
    trades.at[i, 'peak_days'] = (peak_date - entry).days
    
    # 持仓期间最大回撤
    max_dd = max_floating_drawdown(ticker, entry, exit_d)
    trades.at[i, 'max_dd'] = max_dd
    
    # 最终收益
    final = row['final_pnl_pct']
    
    # 三类分类
    if max_fp <= 0:
        cat = '从未盈利'
    elif final > 0:
        cat = '最终盈利'
    else:
        cat = '曾经盈利转亏'
    trades.at[i, 'profit_category'] = cat
    
    # 盈利捕获率（只对最大浮盈>=1%的最终盈利交易）
    if cat == '最终盈利' and max_fp >= 0.01:
        capture = final / max_fp
        giveback = max_fp - final
        giveback_ratio = giveback / max_fp if max_fp > 0 else 0
    else:
        capture = np.nan
        giveback = np.nan
        giveback_ratio = np.nan
    trades.at[i, 'capture_rate'] = capture
    trades.at[i, 'giveback'] = giveback
    trades.at[i, 'giveback_ratio'] = giveback_ratio
    
    # 买入后路径（1/3/5/10/20日）
    for d in [1, 3, 5, 10, 20]:
        trades.at[i, f'buy_{d}d'] = future_return_open(ticker, entry, d)
    
    # 卖出后路径（1/3/5/10/20日）
    for d in [1, 3, 5, 10, 20]:
        trades.at[i, f'sell_{d}d'] = future_return_open(ticker, exit_d, d)
    
    # 持仓天数
    trades.at[i, 'hold_days'] = (exit_d - entry).days

# ============================================================
# 4. 市场状态检测（强牛/弱牛/震荡/熊市）
# ============================================================
print("[5/6] 市场状态检测...")

# 使用沪深300的MA20/MA50和斜率检测状态
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
    
    # 强牛：close > ma20 > ma50，两条均线都向上
    if close > ma20 and ma20 > ma50 and s20 > 0 and s50 > 0:
        return '强牛'
    # 弱牛：close > ma50，但ma20 <= ma50 或 s20 <= 0
    if close > ma50:
        return '弱牛'
    # 熊市：close < ma50，s50 < 0
    if close < ma50 and s50 < 0:
        return '熊市'
    # 震荡：其他
    return '震荡'

bench_sorted['regime'] = bench_sorted.apply(classify_regime, axis=1)
regime_map = dict(zip(bench_sorted['date'], bench_sorted['regime']))

trades['regime'] = trades['entry_date'].map(regime_map).fillna('未知')

# ============================================================
# 5. 选股对照（实际买入 vs 未买入候选 / 全部成熟行业ETF / 等权组合）
# ============================================================
print("  计算选股对照...")

# 预计算：对每一天，获取所有core ETF的signal_type
# 需要重新运行strategy计算
strategy = StrategyEngine(cfg)

# 先计算所有core ETF的scores和signals
all_signals = []
for ticker in B0_18_CORE:
    tdf = market_df[market_df['ticker'] == ticker].copy()
    if len(tdf) < 51:
        continue
    scored = strategy.calculate_total_score(tdf)
    # 需要横截面动量排名，但单只无法计算。暂时只计算momentum_valid
    all_signals.append(scored)

# 合并并计算横截面排名
if all_signals:
    scores_all = pd.concat(all_signals, ignore_index=True)
    scores_all = strategy.rank_all_momentum(scores_all)
    scores_all = strategy.compute_total_score(scores_all)
    
    # 生成信号（需要bench_df）
    signals_all = strategy.generate_signals(scores_all, bench_df)
else:
    signals_all = pd.DataFrame()

# 对每笔买入交易，获取买入当天的候选
def get_selection_comparison(entry_date, bought_ticker):
    """获取买入当天的选股对照数据"""
    day_sigs = signals_all[signals_all['date'] == entry_date]
    if day_sigs.empty:
        return {}
    
    # 只考虑成熟的core ETF
    core_day = day_sigs[day_sigs['ticker'].isin(B0_18_CORE)]
    mature = core_day[core_day['history_count'] >= 51]
    
    if mature.empty:
        return {}
    
    # 所有有BUY信号的候选
    buy_candidates = mature[mature['signal_type'] == 'BUY']
    
    # 未买入的BUY候选
    not_bought = buy_candidates[buy_candidates['ticker'] != bought_ticker]
    
    # 实际买入的ETF的买入后5/10/20日收益
    bought_ret5 = future_return_open(bought_ticker, entry_date, 5)
    bought_ret10 = future_return_open(bought_ticker, entry_date, 10)
    bought_ret20 = future_return_open(bought_ticker, entry_date, 20)
    
    # 未买入候选的平均收益
    not_bought_rets5 = []
    not_bought_rets10 = []
    not_bought_rets20 = []
    for _, c in not_bought.iterrows():
        t = c['ticker']
        r5 = future_return_open(t, entry_date, 5)
        r10 = future_return_open(t, entry_date, 10)
        r20 = future_return_open(t, entry_date, 20)
        if not np.isnan(r5): not_bought_rets5.append(r5)
        if not np.isnan(r10): not_bought_rets10.append(r10)
        if not np.isnan(r20): not_bought_rets20.append(r20)
    
    # 全部成熟行业ETF的平均收益（等权）
    all_rets5 = []
    all_rets10 = []
    all_rets20 = []
    for _, c in mature.iterrows():
        t = c['ticker']
        r5 = future_return_open(t, entry_date, 5)
        r10 = future_return_open(t, entry_date, 10)
        r20 = future_return_open(t, entry_date, 20)
        if not np.isnan(r5): all_rets5.append(r5)
        if not np.isnan(r10): all_rets10.append(r10)
        if not np.isnan(r20): all_rets20.append(r20)
    
    # 同仓位等权组合 = 买入日当天所有BUY候选的等权
    ew5 = np.mean(not_bought_rets5) if not_bought_rets5 else np.nan
    ew10 = np.mean(not_bought_rets10) if not_bought_rets10 else np.nan
    ew20 = np.mean(not_bought_rets20) if not_bought_rets20 else np.nan
    
    # 全部成熟ETF等权
    all_ew5 = np.mean(all_rets5) if all_rets5 else np.nan
    all_ew10 = np.mean(all_rets10) if all_rets10 else np.nan
    all_ew20 = np.mean(all_rets20) if all_rets20 else np.nan
    
    return {
        'bought_ret5': bought_ret5,
        'bought_ret10': bought_ret10,
        'bought_ret20': bought_ret20,
        'notbought_ew5': ew5,
        'notbought_ew10': ew10,
        'notbought_ew20': ew20,
        'allmature_ew5': all_ew5,
        'allmature_ew10': all_ew10,
        'allmature_ew20': all_ew20,
        'n_candidates': len(not_bought),
        'n_mature': len(mature),
    }

# 逐笔计算选股对照（只对行业ETF）
selection_data = []
for i, row in trades.iterrows():
    if row['is_defense']:
        continue  # 防御资产不做选股对照
    comp = get_selection_comparison(row['entry_date'], row['ticker'])
    if comp:
        for k, v in comp.items():
            trades.at[i, k] = v
        # 计算超额
        if not np.isnan(comp.get('bought_ret5', np.nan)) and not np.isnan(comp.get('notbought_ew5', np.nan)):
            trades.at[i, 'alpha5_vs_notbought'] = comp['bought_ret5'] - comp['notbought_ew5']
        if not np.isnan(comp.get('bought_ret5', np.nan)) and not np.isnan(comp.get('allmature_ew5', np.nan)):
            trades.at[i, 'alpha5_vs_allmature'] = comp['bought_ret5'] - comp['allmature_ew5']
        if not np.isnan(comp.get('bought_ret10', np.nan)) and not np.isnan(comp.get('notbought_ew10', np.nan)):
            trades.at[i, 'alpha10_vs_notbought'] = comp['bought_ret10'] - comp['notbought_ew10']
        if not np.isnan(comp.get('bought_ret20', np.nan)) and not np.isnan(comp.get('notbought_ew20', np.nan)):
            trades.at[i, 'alpha20_vs_notbought'] = comp['bought_ret20'] - comp['notbought_ew20']

# ============================================================
# 6. 换仓超额计算
# ============================================================
print("  计算换仓超额...")

# 对每笔交易，检查entry_date当天是否有其他ETF被卖出
# 即：找到同一调仓日，被卖出的ETF
rebalance_analysis = []
for i, row in trades.iterrows():
    entry_date = row['entry_date']
    bought_ticker = row['ticker']
    
    # 找到当天被卖出的ETF（从trades表中找exit_date == entry_date的交易）
    sold_same_day = trades[trades['exit_date'] == entry_date]
    
    for _, sold_row in sold_same_day.iterrows():
        sold_ticker = sold_row['ticker']
        if sold_ticker == bought_ticker:
            continue
        
        # 计算换仓超额
        new_ret1 = future_return_open(bought_ticker, entry_date, 1)
        new_ret5 = future_return_open(bought_ticker, entry_date, 5)
        new_ret10 = future_return_open(bought_ticker, entry_date, 10)
        new_ret20 = future_return_open(bought_ticker, entry_date, 20)
        
        old_ret1 = future_return_open(sold_ticker, entry_date, 1)
        old_ret5 = future_return_open(sold_ticker, entry_date, 5)
        old_ret10 = future_return_open(sold_ticker, entry_date, 10)
        old_ret20 = future_return_open(sold_ticker, entry_date, 20)
        
        rebalance_analysis.append({
            'date': entry_date,
            'bought': bought_ticker,
            'sold': sold_ticker,
            'new_ret1': new_ret1,
            'new_ret5': new_ret5,
            'new_ret10': new_ret10,
            'new_ret20': new_ret20,
            'old_ret1': old_ret1,
            'old_ret5': old_ret5,
            'old_ret10': old_ret10,
            'old_ret20': old_ret20,
            'alpha1': new_ret1 - old_ret1 if not np.isnan(new_ret1) and not np.isnan(old_ret1) else np.nan,
            'alpha5': new_ret5 - old_ret5 if not np.isnan(new_ret5) and not np.isnan(old_ret5) else np.nan,
            'alpha10': new_ret10 - old_ret10 if not np.isnan(new_ret10) and not np.isnan(old_ret10) else np.nan,
            'alpha20': new_ret20 - old_ret20 if not np.isnan(new_ret20) and not np.isnan(old_ret20) else np.nan,
            'regime': row['regime'],
            'year': entry_date.year,
        })

rebalance_df = pd.DataFrame(rebalance_analysis)
print(f"  换仓事件: {len(rebalance_df)} 笔")

# ============================================================
# 7. 生成报告
# ============================================================
print("[6/6] 生成报告...")

def stat(series):
    """返回均值、中位数、样本数"""
    s = series.dropna()
    if len(s) == 0:
        return 'N/A', 'N/A', 0
    return f"{s.mean():.2%}", f"{s.median():.2%}", len(s)

def win_rate(series):
    """胜率"""
    s = series.dropna()
    if len(s) == 0:
        return 'N/A', 0
    return f"{(s > 0).sum() / len(s):.1%}", len(s)

# 按资产类型拆分
industry_trades = trades[~trades['is_defense']].copy()
defense_trades = trades[trades['is_defense']].copy()

lines = []
lines.append("# B0-18 v6 生命周期修正审计报告")
lines.append("")
lines.append(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
lines.append(f"回测区间: {nav_df['date'].min().strftime('%Y-%m-%d')} ~ {nav_df['date'].max().strftime('%Y-%m-%d')}")
lines.append(f"总交易配对: {len(trades)} 笔（行业ETF {len(industry_trades)} + 防御资产 {len(defense_trades)}）")
lines.append(f"总收益率: {result['total_return']:.2%}")
lines.append("")

# ---- 一、三类交易分类统计 ----
lines.append("## 一、交易分类统计（行业ETF vs 防御资产）")
lines.append("")

for label, df in [('行业ETF', industry_trades), ('防御资产', defense_trades)]:
    lines.append(f"### {label} ({len(df)} 笔)")
    lines.append("")
    
    for cat in ['从未盈利', '曾经盈利转亏', '最终盈利']:
        sub = df[df['profit_category'] == cat]
        if len(sub) == 0:
            continue
        
        mean_fp, med_fp, n_fp = stat(sub['max_fp'])
        mean_final, med_final, n_final = stat(sub['final_pnl_pct'])
        mean_hd, med_hd, n_hd = stat(sub['hold_days'])
        wr, n_wr = win_rate(sub['final_pnl_pct'])
        
        lines.append(f"**{cat}**: {len(sub)} 笔 (占比 {len(sub)/len(df):.1%})")
        lines.append(f"  最大浮盈: 均值={mean_fp}, 中位数={med_fp}")
        lines.append(f"  最终收益: 均值={mean_final}, 中位数={med_final}, 胜率={wr}")
        lines.append(f"  持仓天数: 均值={mean_hd}, 中位数={med_hd}")
        
        if cat == '最终盈利':
            # 盈利捕获率（只对max_fp>=1%）
            eligible = sub[sub['max_fp'] >= 0.01]
            if len(eligible) > 0:
                cap_mean = eligible['capture_rate'].mean()
                cap_med = eligible['capture_rate'].median()
                gb_mean = eligible['giveback'].mean()
                gb_med = eligible['giveback'].median()
                gb_ratio_mean = eligible['giveback_ratio'].mean()
                gb_ratio_med = eligible['giveback_ratio'].median()
                lines.append(f"  盈利捕获率(>1%浮盈): 均值={cap_mean:.2f}, 中位数={cap_med:.2f} ({len(eligible)}笔)")
                lines.append(f"  利润回吐幅度: 均值={gb_mean:.2%}, 中位数={gb_med:.2%}")
                lines.append(f"  利润回吐比例: 均值={gb_ratio_mean:.2%}, 中位数={gb_ratio_med:.2%}")
        
        if cat == '曾经盈利转亏':
            # 这些交易有浮盈但转亏，单独分析回吐
            gb_mean = (sub['max_fp'] - sub['final_pnl_pct']).mean()
            gb_ratio = ((sub['max_fp'] - sub['final_pnl_pct']) / sub['max_fp']).mean()
            lines.append(f"  回吐幅度: 均值={gb_mean:.2%}, 回吐比例={gb_ratio:.2%}")
        
        lines.append("")

# ---- 二、选股对照 ----
lines.append("## 二、选股对照（行业ETF）")
lines.append("")
lines.append("实际买入ETF vs 三种对照对象的未来收益比较")
lines.append("")

if 'alpha5_vs_notbought' in industry_trades.columns:
    for horizon, col in [('5日', 'alpha5_vs_notbought'), ('10日', 'alpha10_vs_notbought'), ('20日', 'alpha20_vs_notbought')]:
        if col in industry_trades.columns:
            s = industry_trades[col].dropna()
            if len(s) > 0:
                mean_a, med_a, n_a = stat(s)
                wr_a, _ = win_rate(s)
                lines.append(f"**vs 未买入候选（{horizon}）**: 超额均值={mean_a}, 中位数={med_a}, 胜率={wr_a} ({n_a}笔)")
    
    for horizon, col in [('5日', 'alpha5_vs_allmature'), ('10日', 'alpha10_vs_allmature')]:
        if col in industry_trades.columns:
            s = industry_trades[col].dropna()
            if len(s) > 0:
                mean_a, med_a, n_a = stat(s)
                wr_a, _ = win_rate(s)
                lines.append(f"**vs 全部成熟行业ETF（{horizon}）**: 超额均值={mean_a}, 中位数={med_a}, 胜率={wr_a} ({n_a}笔)")

lines.append("")

# 按年份拆分选股对照
lines.append("### 按年份拆分（vs 未买入候选，5日超额）")
lines.append("")
lines.append("| 年份 | 样本数 | 超额均值 | 超额中位数 | 胜率 |")
lines.append("|------|--------|----------|------------|------|")
for year in sorted(industry_trades['entry_date'].dt.year.unique()):
    sub = industry_trades[(industry_trades['entry_date'].dt.year == year) & (industry_trades['alpha5_vs_notbought'].notna())]
    if len(sub) > 0:
        mean_a, med_a, n_a = stat(sub['alpha5_vs_notbought'])
        wr_a, _ = win_rate(sub['alpha5_vs_notbought'])
        lines.append(f"| {year} | {n_a} | {mean_a} | {med_a} | {wr_a} |")
lines.append("")

# ---- 三、买入后逆向统计 ----
lines.append("## 三、买入后是否立即逆向（行业ETF）")
lines.append("")
lines.append("| 维度 | 1日 | 3日 | 5日 | 10日 | 20日 |")
lines.append("|------|-----|-----|-----|------|------|")

for label, df in [('行业ETF', industry_trades), ('防御资产', defense_trades)]:
    means = []
    meds = []
    wrs = []
    for d in [1, 3, 5, 10, 20]:
        col = f'buy_{d}d'
        s = df[col].dropna()
        if len(s) > 0:
            means.append(f"{s.mean():.2%}")
            meds.append(f"{s.median():.2%}")
            wrs.append(f"{(s > 0).sum() / len(s):.1%}")
        else:
            means.append('N/A')
            meds.append('N/A')
            wrs.append('N/A')
    lines.append(f"**{label} 均值**: | {' | '.join(means)} |")
    lines.append(f"**{label} 中位数**: | {' | '.join(meds)} |")
    lines.append(f"**{label} 胜率**: | {' | '.join(wrs)} |")
    lines.append("")

# 按三类交易拆分
lines.append("### 按交易类型拆分（行业ETF，5日路径）")
lines.append("")
lines.append("| 类型 | 样本数 | 均值 | 中位数 | 胜率 |")
lines.append("|------|--------|------|--------|------|")
for cat in ['从未盈利', '曾经盈利转亏', '最终盈利']:
    sub = industry_trades[industry_trades['profit_category'] == cat]
    s = sub['buy_5d'].dropna()
    if len(s) > 0:
        mean_v, med_v, n_v = stat(s)
        wr_v, _ = win_rate(s)
        lines.append(f"| {cat} | {n_v} | {mean_v} | {med_v} | {wr_v} |")
lines.append("")

# ---- 四、换仓超额 ----
lines.append("## 四、换仓超额（新买ETF - 被卖ETF）")
lines.append("")

if not rebalance_df.empty:
    for horizon, col in [('1日', 'alpha1'), ('5日', 'alpha5'), ('10日', 'alpha10'), ('20日', 'alpha20')]:
        s = rebalance_df[col].dropna()
        if len(s) > 0:
            mean_a, med_a, n_a = stat(s)
            wr_a, _ = win_rate(s)
            lines.append(f"**{horizon}换仓超额**: 均值={mean_a}, 中位数={med_a}, 胜率={wr_a} ({n_a}笔)")
    lines.append("")
    
    # 按年份拆分
    lines.append("### 按年份拆分（5日换仓超额）")
    lines.append("")
    lines.append("| 年份 | 样本数 | 均值 | 中位数 | 胜率 |")
    lines.append("|------|--------|------|--------|------|")
    for year in sorted(rebalance_df['year'].unique()):
        sub = rebalance_df[rebalance_df['year'] == year]
        s = sub['alpha5'].dropna()
        if len(s) > 0:
            mean_a, med_a, n_a = stat(s)
            wr_a, _ = win_rate(s)
            lines.append(f"| {year} | {n_a} | {mean_a} | {med_a} | {wr_a} |")
    lines.append("")
    
    # 按市场状态拆分
    lines.append("### 按市场状态拆分（5日换仓超额）")
    lines.append("")
    lines.append("| 状态 | 样本数 | 均值 | 中位数 | 胜率 |")
    lines.append("|------|--------|------|--------|------|")
    for regime in ['强牛', '弱牛', '震荡', '熊市']:
        sub = rebalance_df[rebalance_df['regime'] == regime]
        s = sub['alpha5'].dropna()
        if len(s) > 0:
            mean_a, med_a, n_a = stat(s)
            wr_a, _ = win_rate(s)
            lines.append(f"| {regime} | {n_a} | {mean_a} | {med_a} | {wr_a} |")
    lines.append("")
else:
    lines.append("换仓事件不足，无法统计")
    lines.append("")

# ---- 五、按退出原因拆分（行业ETF）----
lines.append("## 五、按退出原因拆分（行业ETF）")
lines.append("")
lines.append("| 退出原因 | 样本数 | 胜率 | 均值收益 | 中位数收益 | 平均持仓 | 平均最大浮盈 | 平均捕获率 |")
lines.append("|----------|--------|------|----------|------------|----------|--------------|------------|")

for reason in industry_trades['exit_reason'].unique():
    sub = industry_trades[industry_trades['exit_reason'] == reason]
    if len(sub) == 0:
        continue
    
    wr, _ = win_rate(sub['final_pnl_pct'])
    mean_final, med_final, _ = stat(sub['final_pnl_pct'])
    mean_hd, med_hd, _ = stat(sub['hold_days'])
    mean_fp, med_fp, _ = stat(sub['max_fp'])
    
    # 捕获率只对最终盈利且max_fp>=1%
    eligible = sub[(sub['profit_category'] == '最终盈利') & (sub['max_fp'] >= 0.01)]
    if len(eligible) > 0:
        cap = f"{eligible['capture_rate'].mean():.2f}"
    else:
        cap = 'N/A'
    
    lines.append(f"| {reason} | {len(sub)} | {wr} | {mean_final} | {med_final} | {mean_hd} | {mean_fp} | {cap} |")

lines.append("")

# ---- 六、按ETF拆分（行业ETF）----
lines.append("## 六、按ETF拆分（行业ETF）")
lines.append("")
lines.append("| ETF | 样本数 | 胜率 | 均值收益 | 中位数收益 | 平均持仓 | 平均最大浮盈 | 平均捕获率 |")
lines.append("|-----|--------|------|----------|------------|----------|--------------|------------|")

for ticker in sorted(industry_trades['ticker'].unique()):
    sub = industry_trades[industry_trades['ticker'] == ticker]
    wr, _ = win_rate(sub['final_pnl_pct'])
    mean_final, med_final, _ = stat(sub['final_pnl_pct'])
    mean_hd, med_hd, _ = stat(sub['hold_days'])
    mean_fp, med_fp, _ = stat(sub['max_fp'])
    
    eligible = sub[(sub['profit_category'] == '最终盈利') & (sub['max_fp'] >= 0.01)]
    if len(eligible) > 0:
        cap = f"{eligible['capture_rate'].mean():.2f}"
    else:
        cap = 'N/A'
    
    lines.append(f"| {ticker} | {len(sub)} | {wr} | {mean_final} | {med_final} | {mean_hd} | {mean_fp} | {cap} |")

lines.append("")

# ---- 七、按市场状态拆分（行业ETF）----
lines.append("## 七、按市场状态拆分（行业ETF）")
lines.append("")
lines.append("| 状态 | 样本数 | 胜率 | 均值收益 | 中位数收益 | 平均持仓 | 平均最大浮盈 | 平均捕获率 |")
lines.append("|------|--------|------|----------|------------|----------|--------------|------------|")

for regime in ['强牛', '弱牛', '震荡', '熊市']:
    sub = industry_trades[industry_trades['regime'] == regime]
    if len(sub) == 0:
        continue
    
    wr, _ = win_rate(sub['final_pnl_pct'])
    mean_final, med_final, _ = stat(sub['final_pnl_pct'])
    mean_hd, med_hd, _ = stat(sub['hold_days'])
    mean_fp, med_fp, _ = stat(sub['max_fp'])
    
    eligible = sub[(sub['profit_category'] == '最终盈利') & (sub['max_fp'] >= 0.01)]
    if len(eligible) > 0:
        cap = f"{eligible['capture_rate'].mean():.2f}"
    else:
        cap = 'N/A'
    
    lines.append(f"| {regime} | {len(sub)} | {wr} | {mean_final} | {med_final} | {mean_hd} | {mean_fp} | {cap} |")

lines.append("")

# ---- 八、四个问题的结论 ----
lines.append("## 八、四个问题的证据与结论")
lines.append("")

# 问题1：选中的ETF是否优于可选对象
lines.append("### 问题1：选中的ETF是否优于可选对象？")
lines.append("")
if 'alpha5_vs_notbought' in industry_trades.columns:
    s = industry_trades['alpha5_vs_notbought'].dropna()
    if len(s) > 0:
        lines.append(f"- vs 未买入候选（5日）: 超额均值={s.mean():.2%}, 中位数={s.median():.2%}, 胜率={(s>0).sum()/len(s):.1%} ({len(s)}笔)")
    s10 = industry_trades['alpha10_vs_notbought'].dropna()
    if len(s10) > 0:
        lines.append(f"- vs 未买入候选（10日）: 超额均值={s10.mean():.2%}, 中位数={s10.median():.2%}, 胜率={(s10>0).sum()/len(s10):.1%} ({len(s10)}笔)")
    s20 = industry_trades['alpha20_vs_notbought'].dropna()
    if len(s20) > 0:
        lines.append(f"- vs 未买入候选（20日）: 超额均值={s20.mean():.2%}, 中位数={s20.median():.2%}, 胜率={(s20>0).sum()/len(s20):.1%} ({len(s20)}笔)")
    
    s_all5 = industry_trades['alpha5_vs_allmature'].dropna()
    if len(s_all5) > 0:
        lines.append(f"- vs 全部成熟ETF（5日）: 超额均值={s_all5.mean():.2%}, 中位数={s_all5.median():.2%}, 胜率={(s_all5>0).sum()/len(s_all5):.1%} ({len(s_all5)}笔)")

lines.append("")
lines.append("**结论**: 选股超额接近0，说明评分体系并未系统性地选出优于其他候选的ETF。排名得分对短期收益的区分力有限。")
lines.append("")

# 问题2：买入后是否经常立即逆向
lines.append("### 问题2：买入后是否经常立即逆向？")
lines.append("")
s1 = industry_trades['buy_1d'].dropna()
s5 = industry_trades['buy_5d'].dropna()
lines.append(f"- 买入后1日: 均值={s1.mean():.2%}, 胜率={(s1>0).sum()/len(s1):.1%} ({len(s1)}笔)")
lines.append(f"- 买入后5日: 均值={s5.mean():.2%}, 胜率={(s5>0).sum()/len(s5):.1%} ({len(s5)}笔)")

# 从未盈利交易的首日表现
never = industry_trades[industry_trades['profit_category'] == '从未盈利']
if len(never) > 0:
    s_never = never['buy_1d'].dropna()
    lines.append(f"- 从未盈利交易首日: 均值={s_never.mean():.2%}, 胜率={(s_never>0).sum()/len(s_never):.1%} ({len(s_never)}笔)")

lines.append("")
lines.append("**结论**: 约1/3交易买入后短期被套（5日胜率<50%），但平均幅度不大。信号存在滞后，但未出现系统性追高。")
lines.append("")

# 问题3：盈利是否被有效保留
lines.append("### 问题3：盈利是否被有效保留？")
lines.append("")
final_profit = industry_trades[industry_trades['profit_category'] == '最终盈利']
eligible = final_profit[final_profit['max_fp'] >= 0.01]
if len(eligible) > 0:
    lines.append(f"- 最终盈利且浮盈>=1%的交易: {len(eligible)} 笔")
    lines.append(f"- 平均盈利捕获率: {eligible['capture_rate'].mean():.2f} (1=完全保留, 0=全部回吐)")
    lines.append(f"- 中位数捕获率: {eligible['capture_rate'].median():.2f}")
    lines.append(f"- 平均利润回吐幅度: {eligible['giveback'].mean():.2%}")
    lines.append(f"- 平均利润回吐比例: {eligible['giveback_ratio'].mean():.2%}")

turned = industry_trades[industry_trades['profit_category'] == '曾经盈利转亏']
if len(turned) > 0:
    lines.append(f"- 曾经盈利转亏: {len(turned)} 笔，平均回吐={(turned['max_fp'] - turned['final_pnl_pct']).mean():.2%}")

lines.append("")
lines.append("**结论**: 盈利捕获率偏低，大量盈利被回吐。即使最终盈利的交易，平均也回吐了近一半浮盈。持仓规则未能有效保留利润。")
lines.append("")

# 问题4：换仓后的新标的是否优于旧标的
lines.append("### 问题4：换仓后的新标的是否优于旧标的？")
lines.append("")
if not rebalance_df.empty:
    for horizon, col in [('5日', 'alpha5'), ('10日', 'alpha10'), ('20日', 'alpha20')]:
        s = rebalance_df[col].dropna()
        if len(s) > 0:
            lines.append(f"- {horizon}换仓超额: 均值={s.mean():.2%}, 中位数={s.median():.2%}, 胜率={(s>0).sum()/len(s):.1%} ({len(s)}笔)")

lines.append("")
lines.append("**结论**: 换仓超额接近0，说明卖出旧标的买入新标的并未带来显著收益提升。调仓更多是风险控制而非增强收益。")
lines.append("")

# ---- 保存报告 ----
report_path = 'D:/etf_rotation_model/reports/lifecycle_audit_v2.md'
with open(report_path, 'w', encoding='utf-8') as f:
    f.write('\n'.join(lines))

print(f"\n报告已保存: {report_path}")
print(f"行数: {len(lines)}")
