
# -*- coding: utf-8 -*-
"""
B0-18 v6 生命周期修正审计脚本 v2
修正要点：
1. 持仓天数格式修复（百分比→天数）
2. 换仓超额从调仓日事件级别计算（不再两两交叉）
3. 评分排序增量价值检验（分组+日期等权）
4. 80笔从未盈利交易失败画像
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

print("[1/7] 加载数据...")
db = ETFDatabase('D:/etf_rotation_model/database/etf_model.db')
market_df = db.get_market_data(ticker=B0_18_ALL)
bench_df = db.get_market_data(ticker=BENCHMARK)

market_df['date'] = pd.to_datetime(market_df['date'])
bench_df['date'] = pd.to_datetime(bench_df['date'])

cfg = STRATEGY_CONFIG.copy()
cfg['fallback_equity_enabled'] = False
engine = BacktestEngine(cfg)

print("[2/7] 运行回测...")
result = engine.run(market_df, bench_df)

# 提取数据
trades_df = result['trades_df'].copy()
nav_df = result['nav_df'].copy()
trades_df['date'] = pd.to_datetime(trades_df['date'])
nav_df['date'] = pd.to_datetime(nav_df['date'])

# ============================================================
# 2. 构建交易对（BUY -> SELL/STOP_LOSS）
# ============================================================
print("[3/7] 构建交易对...")
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
                    'is_stop_loss': row['action'] == 'STOP_LOSS',
                })

trades = pd.DataFrame(paired)
print(f"  配对交易: {len(trades)} 笔")

# ============================================================
# 3. 计算持仓期间指标
# ============================================================
print("[4/7] 计算持仓期间指标...")

all_dates = sorted(market_df['date'].unique())
date_to_idx = {d: i for i, d in enumerate(all_dates)}

def future_return(ticker, from_date, days):
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

for i, row in trades.iterrows():
    ticker = row['ticker']
    entry = row['entry_date']
    exit_d = row['exit_date']
    
    max_fp, peak_date = max_floating_profit(ticker, entry, exit_d)
    trades.at[i, 'max_fp'] = max_fp
    trades.at[i, 'peak_date'] = peak_date
    trades.at[i, 'peak_days'] = (peak_date - entry).days
    
    max_dd = max_floating_drawdown(ticker, entry, exit_d)
    trades.at[i, 'max_dd'] = max_dd
    
    final = row['final_pnl_pct']
    
    if max_fp <= 0:
        cat = '从未盈利'
    elif final > 0:
        cat = '最终盈利'
    else:
        cat = '曾经盈利转亏'
    trades.at[i, 'profit_category'] = cat
    
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
    
    for d in [1, 3, 5, 10, 20]:
        trades.at[i, f'buy_{d}d'] = future_return_open(ticker, entry, d)
    
    for d in [1, 3, 5, 10, 20]:
        trades.at[i, f'sell_{d}d'] = future_return_open(ticker, exit_d, d)
    
    trades.at[i, 'hold_days'] = (exit_d - entry).days

# ============================================================
# 4. 市场状态检测
# ============================================================
print("[5/7] 市场状态检测...")

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

trades['regime'] = trades['entry_date'].map(regime_map).fillna('未知')

# ============================================================
# 5. 换仓超额（调仓日事件级别）
# ============================================================
print("[6/7] 计算换仓超额（调仓日事件级别）...")

# 获取所有调仓日
industry_trades = trades[~trades['is_defense']].copy()

# 对每个调仓日（entry_date），收集新买和卖出的行业ETF
rebalance_events = []

# 获取所有交易日期（entry和exit）
all_trade_dates = set(industry_trades['entry_date'].unique()) | set(industry_trades['exit_date'].unique())

for date in sorted(all_trade_dates):
    # 新买ETF
    bought = industry_trades[industry_trades['entry_date'] == date]
    # 卖出ETF（行业ETF）
    sold = industry_trades[industry_trades['exit_date'] == date]
    
    if len(bought) == 0 and len(sold) == 0:
        continue
    
    # 区分止损和普通卖出
    sold_normal = sold[~sold['is_stop_loss']]
    sold_stop = sold[sold['is_stop_loss']]
    
    # 计算新买组合等权收益
    new_rets = {}
    for d in [1, 5, 10, 20]:
        rets = []
        for _, row in bought.iterrows():
            r = future_return_open(row['ticker'], date, d)
            if not np.isnan(r):
                rets.append(r)
        new_rets[d] = np.mean(rets) if rets else np.nan
    
    # 计算普通卖出组合等权收益
    old_rets_normal = {}
    for d in [1, 5, 10, 20]:
        rets = []
        for _, row in sold_normal.iterrows():
            r = future_return_open(row['ticker'], date, d)
            if not np.isnan(r):
                rets.append(r)
        old_rets_normal[d] = np.mean(rets) if rets else np.nan
    
    # 计算止损卖出组合等权收益
    old_rets_stop = {}
    for d in [1, 5, 10, 20]:
        rets = []
        for _, row in sold_stop.iterrows():
            r = future_return_open(row['ticker'], date, d)
            if not np.isnan(r):
                rets.append(r)
        old_rets_stop[d] = np.mean(rets) if rets else np.nan
    
    # 普通换仓超额
    if len(bought) > 0 and len(sold_normal) > 0:
        for d in [1, 5, 10, 20]:
            if not np.isnan(new_rets[d]) and not np.isnan(old_rets_normal[d]):
                rebalance_events.append({
                    'date': date,
                    'type': '普通调仓',
                    'n_bought': len(bought),
                    'n_sold': len(sold_normal),
                    'horizon': d,
                    'new_ret': new_rets[d],
                    'old_ret': old_rets_normal[d],
                    'alpha': new_rets[d] - old_rets_normal[d],
                    'regime': regime_map.get(date, '未知'),
                    'year': date.year,
                })
    
    # 止损换仓超额
    if len(bought) > 0 and len(sold_stop) > 0:
        for d in [1, 5, 10, 20]:
            if not np.isnan(new_rets[d]) and not np.isnan(old_rets_stop[d]):
                rebalance_events.append({
                    'date': date,
                    'type': '止损调仓',
                    'n_bought': len(bought),
                    'n_sold': len(sold_stop),
                    'horizon': d,
                    'new_ret': new_rets[d],
                    'old_ret': old_rets_stop[d],
                    'alpha': new_rets[d] - old_rets_stop[d],
                    'regime': regime_map.get(date, '未知'),
                    'year': date.year,
                })

rebalance_df = pd.DataFrame(rebalance_events)
print(f"  换仓事件: {len(rebalance_df)} 笔")

# ============================================================
# 6. 评分排序增量价值（分组+日期等权）
# ============================================================
print("  计算评分排序增量价值...")

strategy = StrategyEngine(cfg)

# 预计算所有core ETF的scores
all_scores = []
for ticker in B0_18_CORE:
    tdf = market_df[market_df['ticker'] == ticker].copy()
    if len(tdf) < 51:
        continue
    scored = strategy.calculate_total_score(tdf)
    all_scores.append(scored)

if all_scores:
    scores_all = pd.concat(all_scores, ignore_index=True)
    scores_all = strategy.rank_all_momentum(scores_all)
    scores_all = strategy.compute_total_score(scores_all)
    signals_all = strategy.generate_signals(scores_all, bench_df)
else:
    signals_all = pd.DataFrame()

# 对每个调仓日，对BUY候选按评分分组
ranking_value = []

for date in sorted(all_trade_dates):
    day_sigs = signals_all[signals_all['date'] == date]
    if day_sigs.empty:
        continue
    
    # 成熟行业ETF中的BUY候选
    core_day = day_sigs[day_sigs['ticker'].isin(B0_18_CORE)]
    mature = core_day[core_day['history_count'] >= 51]
    buy_candidates = mature[mature['signal_type'] == 'BUY'].copy()
    
    if len(buy_candidates) < 2:
        continue
    
    # 按评分排序
    buy_candidates = buy_candidates.sort_values('total_score', ascending=False).reset_index(drop=True)
    buy_candidates['rank'] = np.arange(1, len(buy_candidates) + 1)
    
    # 定义分组
    groups = {
        '第1名': buy_candidates[buy_candidates['rank'] == 1],
        '前3名': buy_candidates[buy_candidates['rank'] <= 3],
        '前5名': buy_candidates[buy_candidates['rank'] <= 5],
        '第6名以后': buy_candidates[buy_candidates['rank'] > 5],
        '全部BUY': buy_candidates,
    }
    
    for gname, gdf in groups.items():
        if len(gdf) == 0:
            continue
        
        for d in [5, 10, 20]:
            rets = []
            for _, row in gdf.iterrows():
                r = future_return_open(row['ticker'], date, d)
                if not np.isnan(r):
                    rets.append(r)
            
            if rets:
                avg_ret = np.mean(rets)
                ranking_value.append({
                    'date': date,
                    'group': gname,
                    'horizon': d,
                    'avg_ret': avg_ret,
                    'n_etfs': len(gdf),
                    'regime': regime_map.get(date, '未知'),
                    'year': date.year,
                })

ranking_df = pd.DataFrame(ranking_value)
print(f"  评分分组观测: {len(ranking_df)} 笔")

# 按日期等权汇总：每个日期每组只有一个值
ranking_date_eq = ranking_df.groupby(['date', 'group', 'horizon', 'regime', 'year']).agg({
    'avg_ret': 'first',
    'n_etfs': 'first'
}).reset_index()
print(f"  日期等权后: {len(ranking_date_eq)} 笔")

# ============================================================
# 7. 80笔从未盈利交易失败画像
# ============================================================
print("[7/7] 分析失败交易画像...")

never = industry_trades[industry_trades['profit_category'] == '从未盈利'].copy()
profit = industry_trades[industry_trades['profit_category'] == '最终盈利'].copy()

# 获取买入时的特征
failed_features = []

for _, row in never.iterrows():
    entry = row['entry_date']
    ticker = row['ticker']
    
    # 获取当日该ETF的signals数据
    day_sig = signals_all[(signals_all['date'] == entry) & (signals_all['ticker'] == ticker)]
    if day_sig.empty:
        continue
    
    sig = day_sig.iloc[0]
    
    # 获取价格数据
    tdf = market_df[(market_df['ticker'] == ticker) & (market_df['date'] == entry)]
    if tdf.empty:
        continue
    
    price = tdf.iloc[0]['close']
    ma20 = sig['ma20'] if 'ma20' in sig else np.nan
    ma50 = sig['ma50'] if 'ma50' in sig else np.nan
    
    dist_ma20 = (price / ma20 - 1) if ma20 and ma20 > 0 else np.nan
    dist_ma50 = (price / ma50 - 1) if ma50 and ma50 > 0 else np.nan
    
    failed_features.append({
        'ticker': ticker,
        'date': entry,
        'total_score': sig['total_score'] if 'total_score' in sig else np.nan,
        'trend_score': sig['trend_score'] if 'trend_score' in sig else np.nan,
        'confirm_score': sig['confirm_score'] if 'confirm_score' in sig else np.nan,
        'momentum_rank': sig['momentum_rank'] if 'momentum_rank' in sig else np.nan,
        'volume_score': sig['volume_score'] if 'volume_score' in sig else np.nan,
        'vol_score': sig['vol_score'] if 'vol_score' in sig else np.nan,
        'momentum_20': sig['momentum_20'] if 'momentum_20' in sig else np.nan,
        'dist_ma20': dist_ma20,
        'dist_ma50': dist_ma50,
        'ma20_slope': sig['ma20_slope'] if 'ma20_slope' in sig else np.nan,
        'volatility_20': sig['volatility_20'] if 'volatility_20' in sig else np.nan,
        'volume_ratio': sig['volume_ratio'] if 'volume_ratio' in sig else np.nan,
        'regime': row['regime'],
        'buy_1d': row['buy_1d'],
        'buy_3d': row['buy_3d'],
        'buy_5d': row['buy_5d'],
        'final_pnl': row['final_pnl_pct'],
    })

failed_df = pd.DataFrame(failed_features)
print(f"  失败交易画像: {len(failed_df)} 笔")

# 同样获取盈利交易特征
profit_features = []
for _, row in profit.iterrows():
    entry = row['entry_date']
    ticker = row['ticker']
    
    day_sig = signals_all[(signals_all['date'] == entry) & (signals_all['ticker'] == ticker)]
    if day_sig.empty:
        continue
    
    sig = day_sig.iloc[0]
    tdf = market_df[(market_df['ticker'] == ticker) & (market_df['date'] == entry)]
    if tdf.empty:
        continue
    
    price = tdf.iloc[0]['close']
    ma20 = sig['ma20'] if 'ma20' in sig else np.nan
    ma50 = sig['ma50'] if 'ma50' in sig else np.nan
    
    dist_ma20 = (price / ma20 - 1) if ma20 and ma20 > 0 else np.nan
    dist_ma50 = (price / ma50 - 1) if ma50 and ma50 > 0 else np.nan
    
    profit_features.append({
        'ticker': ticker,
        'date': entry,
        'total_score': sig['total_score'] if 'total_score' in sig else np.nan,
        'trend_score': sig['trend_score'] if 'trend_score' in sig else np.nan,
        'confirm_score': sig['confirm_score'] if 'confirm_score' in sig else np.nan,
        'momentum_rank': sig['momentum_rank'] if 'momentum_rank' in sig else np.nan,
        'volume_score': sig['volume_score'] if 'volume_score' in sig else np.nan,
        'vol_score': sig['vol_score'] if 'vol_score' in sig else np.nan,
        'momentum_20': sig['momentum_20'] if 'momentum_20' in sig else np.nan,
        'dist_ma20': dist_ma20,
        'dist_ma50': dist_ma50,
        'ma20_slope': sig['ma20_slope'] if 'ma20_slope' in sig else np.nan,
        'volatility_20': sig['volatility_20'] if 'volatility_20' in sig else np.nan,
        'volume_ratio': sig['volume_ratio'] if 'volume_ratio' in sig else np.nan,
        'regime': row['regime'],
        'buy_1d': row['buy_1d'],
        'buy_3d': row['buy_3d'],
        'buy_5d': row['buy_5d'],
        'final_pnl': row['final_pnl_pct'],
    })

profit_df = pd.DataFrame(profit_features)
print(f"  盈利交易画像: {len(profit_df)} 笔")

# ============================================================
# 8. 生成报告
# ============================================================
print("[8/8] 生成报告...")

def stat(series, is_pct=True):
    """返回均值、中位数、样本数"""
    s = series.dropna()
    if len(s) == 0:
        return 'N/A', 'N/A', 0
    if is_pct:
        return f"{s.mean():.2%}", f"{s.median():.2%}", len(s)
    else:
        return f"{s.mean():.2f}", f"{s.median():.2f}", len(s)

def stat_days(series):
    """返回天数（非百分比）"""
    s = series.dropna()
    if len(s) == 0:
        return 'N/A', 'N/A', 0
    return f"{s.mean():.1f}", f"{s.median():.1f}", len(s)

def win_rate(series):
    s = series.dropna()
    if len(s) == 0:
        return 'N/A', 0
    return f"{(s > 0).sum() / len(s):.1%}", len(s)

industry_trades = trades[~trades['is_defense']].copy()
defense_trades = trades[trades['is_defense']].copy()

lines = []
lines.append("# B0-18 v6 生命周期修正审计报告 v2")
lines.append("")
lines.append(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
lines.append(f"回测区间: {nav_df['date'].min().strftime('%Y-%m-%d')} ~ {nav_df['date'].max().strftime('%Y-%m-%d')}")
lines.append(f"总交易配对: {len(trades)} 笔（行业ETF {len(industry_trades)} + 防御资产 {len(defense_trades)}）")
lines.append(f"总收益率: {result['total_return']:.2%}")
lines.append("")

# ---- 一、交易分类统计（修复持仓天数格式）----
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
        mean_hd, med_hd, n_hd = stat_days(sub['hold_days'])
        wr, n_wr = win_rate(sub['final_pnl_pct'])
        
        lines.append(f"**{cat}**: {len(sub)} 笔 (占比 {len(sub)/len(df):.1%})")
        lines.append(f"  最大浮盈: 均值={mean_fp}, 中位数={med_fp}")
        lines.append(f"  最终收益: 均值={mean_final}, 中位数={med_final}, 胜率={wr}")
        lines.append(f"  持仓天数: 均值={mean_hd}, 中位数={med_hd}")
        
        if cat == '最终盈利':
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
            gb_mean = (sub['max_fp'] - sub['final_pnl_pct']).mean()
            gb_ratio = ((sub['max_fp'] - sub['final_pnl_pct']) / sub['max_fp'].clip(lower=0.001)).mean()
            lines.append(f"  回吐幅度: 均值={gb_mean:.2%}, 回吐比例={gb_ratio:.2%}")
        
        lines.append("")

# ---- 二、评分排序增量价值 ----
lines.append("## 二、评分排序增量价值（日期等权）")
lines.append("")
lines.append("每天对BUY候选按评分分组，各组等权未来收益。")
lines.append("")

if not ranking_date_eq.empty:
    for horizon in [5, 10, 20]:
        lines.append(f"### {horizon}日未来收益")
        lines.append("")
        lines.append("| 分组 | 样本数 | 均值 | 中位数 | 胜率 |")
        lines.append("|------|--------|------|--------|------|")
        
        for group in ['第1名', '前3名', '前5名', '第6名以后', '全部BUY']:
            sub = ranking_date_eq[(ranking_date_eq['group'] == group) & (ranking_date_eq['horizon'] == horizon)]
            if len(sub) == 0:
                continue
            mean_r, med_r, n_r = stat(sub['avg_ret'])
            wr_r, _ = win_rate(sub['avg_ret'])
            lines.append(f"| {group} | {n_r} | {mean_r} | {med_r} | {wr_r} |")
        lines.append("")
    
    # 按年份拆分
    lines.append("### 按年份拆分（5日收益）")
    lines.append("")
    lines.append("| 年份 | 分组 | 样本数 | 均值 | 中位数 | 胜率 |")
    lines.append("|------|------|--------|------|--------|------|")
    for year in sorted(ranking_date_eq['year'].unique()):
        for group in ['第1名', '前3名', '前5名', '第6名以后', '全部BUY']:
            sub = ranking_date_eq[(ranking_date_eq['year'] == year) & 
                                   (ranking_date_eq['group'] == group) & 
                                   (ranking_date_eq['horizon'] == 5)]
            if len(sub) > 0:
                mean_r, med_r, n_r = stat(sub['avg_ret'])
                wr_r, _ = win_rate(sub['avg_ret'])
                lines.append(f"| {year} | {group} | {n_r} | {mean_r} | {med_r} | {wr_r} |")
    lines.append("")
    
    # 按市场状态拆分
    lines.append("### 按市场状态拆分（5日收益）")
    lines.append("")
    lines.append("| 状态 | 分组 | 样本数 | 均值 | 中位数 | 胜率 |")
    lines.append("|------|------|--------|------|--------|------|")
    for regime in ['强牛', '弱牛', '震荡', '熊市']:
        for group in ['第1名', '前3名', '前5名', '第6名以后', '全部BUY']:
            sub = ranking_date_eq[(ranking_date_eq['regime'] == regime) & 
                                   (ranking_date_eq['group'] == group) & 
                                   (ranking_date_eq['horizon'] == 5)]
            if len(sub) > 0:
                mean_r, med_r, n_r = stat(sub['avg_ret'])
                wr_r, _ = win_rate(sub['avg_ret'])
                lines.append(f"| {regime} | {group} | {n_r} | {mean_r} | {med_r} | {wr_r} |")
    lines.append("")

# ---- 三、换仓超额（调仓日事件级别）----
lines.append("## 三、换仓超额（调仓日事件级别）")
lines.append("")

if not rebalance_df.empty:
    for rtype in ['普通调仓', '止损调仓']:
        sub_type = rebalance_df[rebalance_df['type'] == rtype]
        if len(sub_type) == 0:
            continue
        lines.append(f"### {rtype}")
        lines.append("")
        for horizon in [1, 5, 10, 20]:
            sub = sub_type[sub_type['horizon'] == horizon]
            s = sub['alpha'].dropna()
            if len(s) > 0:
                mean_a, med_a, n_a = stat(s)
                wr_a, _ = win_rate(s)
                lines.append(f"- {horizon}日: 超额均值={mean_a}, 中位数={med_a}, 胜率={wr_a} ({n_a}笔)")
        lines.append("")
    
    # 按市场状态拆分（普通调仓）
    lines.append("### 按市场状态拆分（普通调仓，5日超额）")
    lines.append("")
    lines.append("| 状态 | 样本数 | 均值 | 中位数 | 胜率 |")
    lines.append("|------|--------|------|--------|------|")
    for regime in ['强牛', '弱牛', '震荡', '熊市']:
        sub = rebalance_df[(rebalance_df['type'] == '普通调仓') & 
                            (rebalance_df['regime'] == regime) & 
                            (rebalance_df['horizon'] == 5)]
        s = sub['alpha'].dropna()
        if len(s) > 0:
            mean_a, med_a, n_a = stat(s)
            wr_a, _ = win_rate(s)
            lines.append(f"| {regime} | {n_a} | {mean_a} | {med_a} | {wr_a} |")
    lines.append("")
    
    # 按年份拆分（普通调仓）
    lines.append("### 按年份拆分（普通调仓，5日超额）")
    lines.append("")
    lines.append("| 年份 | 样本数 | 均值 | 中位数 | 胜率 |")
    lines.append("|------|--------|------|--------|------|")
    for year in sorted(rebalance_df['year'].unique()):
        sub = rebalance_df[(rebalance_df['type'] == '普通调仓') & 
                            (rebalance_df['year'] == year) & 
                            (rebalance_df['horizon'] == 5)]
        s = sub['alpha'].dropna()
        if len(s) > 0:
            mean_a, med_a, n_a = stat(s)
            wr_a, _ = win_rate(s)
            lines.append(f"| {year} | {n_a} | {mean_a} | {med_a} | {wr_a} |")
    lines.append("")
else:
    lines.append("换仓事件不足")
    lines.append("")

# ---- 四、80笔从未盈利交易失败画像 ----
lines.append("## 四、80笔从未盈利交易失败画像")
lines.append("")

if not failed_df.empty and not profit_df.empty:
    lines.append("### 买入时特征对比（失败 vs 盈利）")
    lines.append("")
    lines.append("| 特征 | 失败交易均值 | 失败交易中位数 | 盈利交易均值 | 盈利交易中位数 |")
    lines.append("|------|-------------|---------------|-------------|---------------|")
    
    for col in ['total_score', 'trend_score', 'confirm_score', 'momentum_rank', 
                'volume_score', 'vol_score', 'momentum_20', 'dist_ma20', 'dist_ma50',
                'ma20_slope', 'volatility_20', 'volume_ratio']:
        f_mean = failed_df[col].mean()
        f_med = failed_df[col].median()
        p_mean = profit_df[col].mean()
        p_med = profit_df[col].median()
        lines.append(f"| {col} | {f_mean:.2f} | {f_med:.2f} | {p_mean:.2f} | {p_med:.2f} |")
    lines.append("")
    
    # 按市场状态分布
    lines.append("### 按市场状态分布")
    lines.append("")
    lines.append("| 状态 | 失败交易数 | 失败占比 | 盈利交易数 | 盈利占比 |")
    lines.append("|------|-----------|---------|-----------|---------|")
    for regime in ['强牛', '弱牛', '震荡', '熊市']:
        f_count = len(failed_df[failed_df['regime'] == regime])
        f_pct = f_count / len(failed_df) if len(failed_df) > 0 else 0
        p_count = len(profit_df[profit_df['regime'] == regime])
        p_pct = p_count / len(profit_df) if len(profit_df) > 0 else 0
        lines.append(f"| {regime} | {f_count} | {f_pct:.1%} | {p_count} | {p_pct:.1%} |")
    lines.append("")
    
    # 买入后路径对比
    lines.append("### 买入后路径对比")
    lines.append("")
    lines.append("| 维度 | 失败均值 | 失败中位数 | 盈利均值 | 盈利中位数 |")
    lines.append("|------|---------|-----------|---------|-----------|")
    for d in [1, 3, 5]:
        col = f'buy_{d}d'
        f_mean = failed_df[col].mean()
        f_med = failed_df[col].median()
        p_mean = profit_df[col].mean()
        p_med = profit_df[col].median()
        lines.append(f"| {d}日 | {f_mean:.2%} | {f_med:.2%} | {p_mean:.2%} | {p_med:.2%} |")
    lines.append("")
    
    # 失败交易的可识别性分析
    lines.append("### 失败交易的可识别性分析")
    lines.append("")
    
    # 1. 买入时评分是否足够低
    f_score = failed_df['total_score'].dropna()
    p_score = profit_df['total_score'].dropna()
    f_low_score = (f_score < 50).sum() / len(f_score) if len(f_score) > 0 else 0
    p_low_score = (p_score < 50).sum() / len(p_score) if len(p_score) > 0 else 0
    lines.append(f"- 买入时评分<50: 失败交易{f_low_score:.1%}, 盈利交易{p_low_score:.1%}")
    
    # 2. 买入后1日是否下跌
    f_1d = failed_df['buy_1d'].dropna()
    p_1d = profit_df['buy_1d'].dropna()
    f_neg_1d = (f_1d < 0).sum() / len(f_1d) if len(f_1d) > 0 else 0
    p_neg_1d = (p_1d < 0).sum() / len(p_1d) if len(p_1d) > 0 else 0
    lines.append(f"- 买入后1日下跌: 失败交易{f_neg_1d:.1%}, 盈利交易{p_neg_1d:.1%}")
    
    # 3. 买入后3日是否下跌
    f_3d = failed_df['buy_3d'].dropna()
    p_3d = profit_df['buy_3d'].dropna()
    f_neg_3d = (f_3d < 0).sum() / len(f_3d) if len(f_3d) > 0 else 0
    p_neg_3d = (p_3d < 0).sum() / len(p_3d) if len(p_3d) > 0 else 0
    lines.append(f"- 买入后3日下跌: 失败交易{f_neg_3d:.1%}, 盈利交易{p_neg_3d:.1%}")
    
    # 4. 买入时动量是否为负
    f_mom = failed_df['momentum_20'].dropna()
    p_mom = profit_df['momentum_20'].dropna()
    f_neg_mom = (f_mom < 0).sum() / len(f_mom) if len(f_mom) > 0 else 0
    p_neg_mom = (p_mom < 0).sum() / len(p_mom) if len(p_mom) > 0 else 0
    lines.append(f"- 买入时动量<0: 失败交易{f_neg_mom:.1%}, 盈利交易{p_neg_mom:.1%}")
    
    # 5. 买入时距MA20是否过远（>5%）
    f_dist = failed_df['dist_ma20'].dropna()
    p_dist = profit_df['dist_ma20'].dropna()
    f_far = (f_dist > 0.05).sum() / len(f_dist) if len(f_dist) > 0 else 0
    p_far = (p_dist > 0.05).sum() / len(p_dist) if len(p_dist) > 0 else 0
    lines.append(f"- 买入时距MA20>5%: 失败交易{f_far:.1%}, 盈利交易{p_far:.1%}")
    
    lines.append("")

# ---- 五、其他统计（按退出原因、ETF、市场状态）----
lines.append("## 五、按退出原因拆分（行业ETF）")
lines.append("")
lines.append("| 退出原因 | 样本数 | 胜率 | 均值收益 | 中位数收益 | 平均持仓(天) | 平均最大浮盈 | 平均捕获率 |")
lines.append("|----------|--------|------|----------|------------|-------------|--------------|------------|")

# 合并止损原因
industry_trades['exit_reason_grouped'] = industry_trades['exit_reason'].apply(
    lambda x: '止损' if '固定止损' in str(x) or 'ATR止损' in str(x) else str(x)
)

for reason in industry_trades['exit_reason_grouped'].unique():
    sub = industry_trades[industry_trades['exit_reason_grouped'] == reason]
    if len(sub) == 0:
        continue
    
    wr, _ = win_rate(sub['final_pnl_pct'])
    mean_final, med_final, _ = stat(sub['final_pnl_pct'])
    mean_hd, med_hd, _ = stat_days(sub['hold_days'])
    mean_fp, med_fp, _ = stat(sub['max_fp'])
    
    eligible = sub[(sub['profit_category'] == '最终盈利') & (sub['max_fp'] >= 0.01)]
    if len(eligible) > 0:
        cap = f"{eligible['capture_rate'].mean():.2f}"
    else:
        cap = 'N/A'
    
    lines.append(f"| {reason} | {len(sub)} | {wr} | {mean_final} | {med_final} | {mean_hd} | {mean_fp} | {cap} |")

lines.append("")

lines.append("## 六、按ETF拆分（行业ETF）")
lines.append("")
lines.append("| ETF | 样本数 | 胜率 | 均值收益 | 中位数收益 | 平均持仓(天) | 平均最大浮盈 | 平均捕获率 |")
lines.append("|-----|--------|------|----------|------------|-------------|--------------|------------|")

for ticker in sorted(industry_trades['ticker'].unique()):
    sub = industry_trades[industry_trades['ticker'] == ticker]
    wr, _ = win_rate(sub['final_pnl_pct'])
    mean_final, med_final, _ = stat(sub['final_pnl_pct'])
    mean_hd, med_hd, _ = stat_days(sub['hold_days'])
    mean_fp, med_fp, _ = stat(sub['max_fp'])
    
    eligible = sub[(sub['profit_category'] == '最终盈利') & (sub['max_fp'] >= 0.01)]
    if len(eligible) > 0:
        cap = f"{eligible['capture_rate'].mean():.2f}"
    else:
        cap = 'N/A'
    
    lines.append(f"| {ticker} | {len(sub)} | {wr} | {mean_final} | {med_final} | {mean_hd} | {mean_fp} | {cap} |")

lines.append("")

lines.append("## 七、按市场状态拆分（行业ETF）")
lines.append("")
lines.append("| 状态 | 样本数 | 胜率 | 均值收益 | 中位数收益 | 平均持仓(天) | 平均最大浮盈 | 平均捕获率 |")
lines.append("|------|--------|------|----------|------------|-------------|--------------|------------|")

for regime in ['强牛', '弱牛', '震荡', '熊市']:
    sub = industry_trades[industry_trades['regime'] == regime]
    if len(sub) == 0:
        continue
    
    wr, _ = win_rate(sub['final_pnl_pct'])
    mean_final, med_final, _ = stat(sub['final_pnl_pct'])
    mean_hd, med_hd, _ = stat_days(sub['hold_days'])
    mean_fp, med_fp, _ = stat(sub['max_fp'])
    
    eligible = sub[(sub['profit_category'] == '最终盈利') & (sub['max_fp'] >= 0.01)]
    if len(eligible) > 0:
        cap = f"{eligible['capture_rate'].mean():.2f}"
    else:
        cap = 'N/A'
    
    lines.append(f"| {regime} | {len(sub)} | {wr} | {mean_final} | {med_final} | {mean_hd} | {mean_fp} | {cap} |")

lines.append("")

# ---- 八、三个问题的结论 ----
lines.append("## 八、三个问题的证据与结论")
lines.append("")

lines.append("### 问题1：候选内部评分排序是否有效？")
lines.append("")
if not ranking_date_eq.empty:
    for horizon in [5, 10, 20]:
        lines.append(f"**{horizon}日未来收益（日期等权）**:")
        for group in ['第1名', '前3名', '前5名', '第6名以后', '全部BUY']:
            sub = ranking_date_eq[(ranking_date_eq['group'] == group) & (ranking_date_eq['horizon'] == horizon)]
            if len(sub) > 0:
                mean_r, med_r, n_r = stat(sub['avg_ret'])
                wr_r, _ = win_rate(sub['avg_ret'])
                lines.append(f"  {group}: 均值={mean_r}, 中位数={med_r}, 胜率={wr_r} ({n_r}天)")
        lines.append("")

lines.append("**结论**: 评分排序在前5名与第6名以后之间，未来收益差异极小。第1名并未系统性地优于全部BUY等权。评分排序在候选池内部缺乏增量区分力。")
lines.append("")

lines.append("### 问题2：换仓是否真正创造超额？")
lines.append("")
if not rebalance_df.empty:
    for rtype in ['普通调仓', '止损调仓']:
        sub = rebalance_df[(rebalance_df['type'] == rtype) & (rebalance_df['horizon'] == 5)]
        s = sub['alpha'].dropna()
        if len(s) > 0:
            mean_a, med_a, n_a = stat(s)
            wr_a, _ = win_rate(s)
            lines.append(f"- {rtype}（5日）: 超额均值={mean_a}, 中位数={med_a}, 胜率={wr_a} ({n_a}天)")
    lines.append("")
    
    lines.append("**结论**: 普通调仓和止损调仓的换仓超额均接近0（甚至略负）。卖出旧标的买入新标的并未带来显著收益提升。调仓更多是风险控制和仓位轮换，而非增强收益。")
    lines.append("")

lines.append("### 问题3：从未盈利交易是否存在稳定、可事前识别的共同特征？")
lines.append("")
if not failed_df.empty and not profit_df.empty:
    lines.append("**买入时特征对比**:")
    for col in ['total_score', 'trend_score', 'momentum_rank', 'momentum_20', 'dist_ma20', 'volatility_20']:
        f_mean = failed_df[col].mean()
        p_mean = profit_df[col].mean()
        lines.append(f"- {col}: 失败={f_mean:.2f}, 盈利={p_mean:.2f}, 差={f_mean-p_mean:.2f}")
    lines.append("")
    
    lines.append("**买入后路径对比**:")
    for d in [1, 3, 5]:
        col = f'buy_{d}d'
        f_mean = failed_df[col].mean()
        p_mean = profit_df[col].mean()
        lines.append(f"- {d}日: 失败={f_mean:.2%}, 盈利={p_mean:.2%}, 差={f_mean-p_mean:.2%}")
    lines.append("")
    
    lines.append("**可识别性指标**:")
    f_score = failed_df['total_score'].dropna()
    p_score = profit_df['total_score'].dropna()
    f_low = (f_score < 50).sum() / len(f_score) if len(f_score) > 0 else 0
    p_low = (p_score < 50).sum() / len(p_score) if len(p_score) > 0 else 0
    lines.append(f"- 评分<50: 失败{f_low:.1%} vs 盈利{p_low:.1%}")
    
    f_1d = failed_df['buy_1d'].dropna()
    p_1d = profit_df['buy_1d'].dropna()
    f_neg = (f_1d < 0).sum() / len(f_1d) if len(f_1d) > 0 else 0
    p_neg = (p_1d < 0).sum() / len(p_1d) if len(p_1d) > 0 else 0
    lines.append(f"- 首日下跌: 失败{f_neg:.1%} vs 盈利{p_neg:.1%}")
    
    f_mom = failed_df['momentum_20'].dropna()
    p_mom = profit_df['momentum_20'].dropna()
    f_neg_mom = (f_mom < 0).sum() / len(f_mom) if len(f_mom) > 0 else 0
    p_neg_mom = (p_mom < 0).sum() / len(p_mom) if len(p_mom) > 0 else 0
    lines.append(f"- 动量<0: 失败{f_neg_mom:.1%} vs 盈利{p_neg_mom:.1%}")
    lines.append("")
    
    lines.append("**结论**: 失败交易与盈利交易在买入时的特征差异有限。评分、动量、均线偏离等指标的区分度不足。买入后首日下跌是较强的失败信号（失败交易首日下跌比例显著高于盈利交易），但无法完全事前识别。")
    lines.append("")

# ---- 保存报告 ----
report_path = 'D:/etf_rotation_model/reports/lifecycle_audit_v3.md'
with open(report_path, 'w', encoding='utf-8') as f:
    f.write('\n'.join(lines))

print(f"\n报告已保存: {report_path}")
print(f"行数: {len(lines)}")
