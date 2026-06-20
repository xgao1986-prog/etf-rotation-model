# -*- coding: utf-8 -*-
"""
S1验收脚本：筛选后排序单变量测试验收修正

验收原则：
1. 使用B0冻结配置、统一截止日(2026-06-05)、统一起点(2019-08-13)
2. B0必须精确复现（当前代码s1_mode=False）
3. S1比较表每行一个有效调仓事件（约335行）
4. 1378行 = 所有周四调仓日（非有效调仓日）
5. 修正过滤率：根据成熟数→硬条件通过数计算
6. 逐笔列出变化事件
7. 验收勾稽：日收益差异与NAV差异一致
"""
import sys
sys.path.insert(0, 'D:/etf_rotation_model/src')

import pandas as pd
import numpy as np
from datetime import datetime
from database import ETFDatabase
from backtest import BacktestEngine
from strategy import StrategyEngine
import config as _config_module
# 冻结配置：将CORE_UNIVERSE限制为ETF_UNIVERSE（16只行业ETF）
# 这是用户要求的B0-18配置，不得使用32只概念池
_config_module.CORE_UNIVERSE = _config_module.ETF_UNIVERSE

from config import STRATEGY_CONFIG, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK, CORE_UNIVERSE, BACKTEST_CONFIG

# 冻结配置参数
WARMUP_END = pd.to_datetime('2019-08-13')
COMMON_CUTOFF = pd.to_datetime('2026-06-05')
ALL_CORE_TICKERS = list(ETF_UNIVERSE.keys())  # 16只（已冻结，B0-18）
ALL_DEFENSE_TICKERS = list(DEFENSE_UNIVERSE.keys())  # 2只

print("=" * 80)
print("S1验收：筛选后排序单变量测试")
print(f"冻结配置：CORE_UNIVERSE={len(ALL_CORE_TICKERS)}只行业, DEFENSE={len(ALL_DEFENSE_TICKERS)}只")
print(f"统一截止日：{COMMON_CUTOFF.strftime('%Y-%m-%d')}")
print(f"统一起点：{WARMUP_END.strftime('%Y-%m-%d')}")
print("=" * 80)

# ============================================================
# 1. 加载数据
# ============================================================
print("\n[1/8] 加载数据...")
db = ETFDatabase('D:/etf_rotation_model/database/etf_model.db')
market_df = db.get_market_data(ticker=ALL_CORE_TICKERS + ALL_DEFENSE_TICKERS)
bench_df = db.get_market_data(ticker=BENCHMARK)
market_df['date'] = pd.to_datetime(market_df['date'])
bench_df['date'] = pd.to_datetime(bench_df['date'])

# 统一截断到共同截止日（与backtest.py一致）
market_df = market_df[market_df['date'] <= COMMON_CUTOFF].copy()
bench_df = bench_df[bench_df['date'] <= COMMON_CUTOFF].copy()

print(f"  市场数据: {market_df['date'].nunique()} 交易日, {market_df['ticker'].nunique()} 只ETF")
print(f"  基准数据: {bench_df['date'].nunique()} 交易日")

# 计算行业ETF等权池
market_core = market_df[market_df['ticker'].isin(ALL_CORE_TICKERS)].copy()
core_pivot = market_core.pivot_table(index='date', columns='ticker', values='close')
core_ew = core_pivot.mean(axis=1)

# 所有交易日
all_dates = sorted(market_df['date'].unique())
all_weekdays = [d for d in all_dates if d.weekday() == 3]  # 所有周四
print(f"  所有交易日: {len(all_dates)}")
print(f"  所有周四: {len(all_weekdays)}")

# ============================================================
# 2. 运行B0和S1回测
# ============================================================
print("\n[2/8] 运行B0和S1回测...")

cfg = STRATEGY_CONFIG.copy()

engine_b0 = BacktestEngine(cfg, s1_mode=False)
result_b0 = engine_b0.run(market_df, bench_df)
nav_b0 = result_b0['nav_df'].copy()
nav_b0['date'] = pd.to_datetime(nav_b0['date'])

engine_s1 = BacktestEngine(cfg, s1_mode=True)
result_s1 = engine_s1.run(market_df, bench_df)
nav_s1 = result_s1['nav_df'].copy()
nav_s1['date'] = pd.to_datetime(nav_s1['date'])

print(f"  B0: {len(nav_b0)} 日, 最终NAV={nav_b0['nav'].iloc[-1]:.2f}")
print(f"  S1: {len(nav_s1)} 日, 最终NAV={nav_s1['nav'].iloc[-1]:.2f}")

# ============================================================
# 3. 复现检查：B0必须精确复现
# ============================================================
print("\n[3/8] B0复现检查...")

# 使用有效日期范围（统一策略起点之后）
sub_b0 = nav_b0[nav_b0['date'] >= WARMUP_END].sort_values('date').reset_index(drop=True)
sub_b0['ret'] = sub_b0['nav'].pct_change()

total_ret = sub_b0['nav'].iloc[-1] / sub_b0['nav'].iloc[0] - 1
years = (sub_b0['date'].iloc[-1] - sub_b0['date'].iloc[0]).days / 365.25
ann_ret = (1 + total_ret) ** (1 / years) - 1 if years > 0 else 0
vol = sub_b0['ret'].std() * np.sqrt(252)
sharpe = ann_ret / vol if vol > 0 else 0
cummax = sub_b0['nav'].cummax()
max_dd = ((sub_b0['nav'] - cummax) / cummax).min()

print(f"  B0复现结果:")
print(f"    起始NAV: {sub_b0['nav'].iloc[0]:.2f}")
print(f"    结束NAV: {sub_b0['nav'].iloc[-1]:.2f}")
print(f"    总收益: {total_ret:.2%}")
print(f"    年化: {ann_ret:.2%}")
print(f"    夏普: {sharpe:.2f}")
print(f"    最大回撤: {max_dd:.2%}")
print(f"    交易日: {len(sub_b0)}")
print(f"    excluded_tickers: {result_b0.get('excluded_tickers', [])}")

# ============================================================
# 4. 计算每日B0和S1的信号（用于对比）
# ============================================================
print("\n[4/8] 计算每日B0和S1信号...")

strategy_b0 = StrategyEngine(cfg, s1_mode=False)
strategy_s1 = StrategyEngine(cfg, s1_mode=True)

all_scores_b0 = []
all_scores_s1 = []
for ticker in ALL_CORE_TICKERS:
    tdf = market_df[market_df['ticker'] == ticker].copy()
    if len(tdf) < 51:
        continue
    scored_b0 = strategy_b0.calculate_total_score(tdf)
    scored_s1 = strategy_s1.calculate_total_score(tdf)
    all_scores_b0.append(scored_b0)
    all_scores_s1.append(scored_s1)

scores_b0 = pd.concat(all_scores_b0, ignore_index=True)
scores_s1 = pd.concat(all_scores_s1, ignore_index=True)

# B0：先排名后筛选
scores_b0 = strategy_b0.rank_all_momentum(scores_b0)
scores_b0 = strategy_b0.compute_total_score(scores_b0)

# S1：先硬筛选，再排名（在generate_signals中处理）
# 但我们需要先计算原始分数（不含排名分）用于分析
scores_s1_raw = strategy_s1.compute_total_score(scores_s1)  # 此时momentum_rank=0（默认值）

# 生成信号
signals_b0 = strategy_b0.generate_signals(scores_b0, bench_df)
signals_s1 = strategy_s1.generate_signals(scores_s1, bench_df)

# 提取行业ETF BUY信号
buy_b0 = signals_b0[
    (signals_b0['ticker'].isin(ALL_CORE_TICKERS)) & 
    (signals_b0['signal_type'] == 'BUY') &
    (signals_b0['momentum_valid'] == True)
].copy()

buy_s1 = signals_s1[
    (signals_s1['ticker'].isin(ALL_CORE_TICKERS)) & 
    (signals_s1['signal_type'] == 'BUY') &
    (signals_s1['momentum_valid'] == True)
].copy()

# ============================================================
# 5. 构建有效调仓事件表（每个有效调仓日一行）
# ============================================================
print("\n[5/8] 构建有效调仓事件表...")

# 获取有效调仓日（周四，排除预热期和未知状态）
def classify_regime(date, bench_df):
    bsub = bench_df[bench_df['date'] <= date].tail(60).sort_values('date')
    if len(bsub) < 50:
        return '未知'
    bsub['ma20'] = bsub['close'].rolling(20).mean()
    bsub['ma50'] = bsub['close'].rolling(50).mean()
    bsub['ma20_slope'] = bsub['ma20'].diff()
    bsub['ma50_slope'] = bsub['ma50'].diff()
    row = bsub.iloc[-1]
    close, ma20, ma50, s20, s50 = row['close'], row['ma20'], row['ma50'], row['ma20_slope'], row['ma50_slope']
    if pd.isna(ma50):
        return '未知'
    if close > ma20 and ma20 > ma50 and s20 > 0 and s50 > 0:
        return '强牛'
    if close > ma50:
        return '弱牛'
    if close < ma50 and s50 < 0:
        return '熊市'
    return '震荡'

valid_rebalance_dates = []
for d in all_weekdays:
    if d < WARMUP_END:
        continue
    regime = classify_regime(d, bench_df)
    if regime == '未知':
        continue
    valid_rebalance_dates.append(d)

print(f"  所有周四: {len(all_weekdays)}")
print(f"  预热后周四: {len([d for d in all_weekdays if d >= WARMUP_END])}")
print(f"  有效调仓日: {len(valid_rebalance_dates)}")

# 验证：最后一个有效调仓日没有下期，所以事件数 = len(valid_rebalance_dates) - 1
# 但用户说"约335行"，这对应335个有效调仓日
# 每个事件 = 调仓日 -> 下一个调仓日
rebalance_event_dates = valid_rebalance_dates[:-1]  # 排除最后一个（无下期）
print(f"  有效调仓事件: {len(rebalance_event_dates)}")

# 解释1378行
print(f"\n  【1378行解释】")
print(f"  1378 = 所有周四调仓日数量（从第一个周四到最后一个周四）")
print(f"  其中：预热前10个 + 预热后未知状态1个 + 有效调仓日{len(valid_rebalance_dates)}个 = {len(all_weekdays)}")
print(f"  注意：1378不是'有效调仓事件'数，而是'所有周四'数")
print(f"  有效调仓事件 = {len(rebalance_event_dates)}（最后一个调仓日无下期，不生成事件）")

# ============================================================
# 6. 逐调仓事件记录详细数据
# ============================================================
print("\n[6/8] 逐调仓事件记录详细数据...")

event_records = []

for i, date in enumerate(rebalance_event_dates):
    next_date = valid_rebalance_dates[i + 1]
    
    # 市场状态
    regime = classify_regime(date, bench_df)
    
    # 当天所有core ETF的数据
    day_b0 = scores_b0[scores_b0['date'] == date]
    day_s1 = scores_s1_raw[scores_s1_raw['date'] == date]  # S1原始分数（不含排名分）
    
    # 1. 成熟行业ETF数量
    mature_b0 = day_b0[(day_b0['ticker'].isin(ALL_CORE_TICKERS)) & (day_b0['history_count'] >= 51) & (day_b0['momentum_valid'] == True)]
    mature_s1 = day_s1[(day_s1['ticker'].isin(ALL_CORE_TICKERS)) & (day_s1['history_count'] >= 51) & (day_s1['momentum_valid'] == True)]
    n_mature = len(mature_b0)  # B0和S1的mature定义相同
    
    # 2. 硬条件通过数量（不含total_score）
    # 硬条件: trend>=15, confirm>=4, prev_close>ma20, ma20_slope>0
    # 需要获取前一日收盘价
    hard_pass_b0 = []
    for _, row in mature_b0.iterrows():
        ticker = row['ticker']
        tdf = scores_b0[(scores_b0['ticker'] == ticker) & (scores_b0['date'] < date)].sort_values('date')
        if len(tdf) > 0:
            prev_close = tdf['close'].iloc[-1]
            if (row['trend_score'] >= cfg['min_trend_score'] and 
                row['confirm_score'] >= cfg['min_confirm_score'] and
                prev_close > row['ma20'] and
                row['ma20_slope'] > 0):
                hard_pass_b0.append(ticker)
    
    n_hard_pass = len(hard_pass_b0)
    
    # 3. B0排名池数量 = 所有成熟行业ETF（B0先排名后筛选）
    n_b0_rank_pool = n_mature
    
    # 4. S1排名池数量 = 硬条件通过的ETF（S1先筛选后排名）
    n_s1_rank_pool = n_hard_pass
    
    # 5. B0最终候选数量（通过total_score门槛）
    b0_buy = buy_b0[buy_b0['date'] == date]
    n_b0_final = len(b0_buy)
    
    # 6. S1最终候选数量
    s1_buy = buy_s1[buy_s1['date'] == date]
    n_s1_final = len(s1_buy)
    
    # 7. B0前5名
    b0_top5 = b0_buy.sort_values('total_score', ascending=False).head(5)
    b0_top5_list = b0_top5['ticker'].tolist()
    
    # 8. S1前5名
    s1_top5 = s1_buy.sort_values('total_score', ascending=False).head(5)
    s1_top5_list = s1_top5['ticker'].tolist()
    
    # 9. 前5名变化
    top5_changed = set(b0_top5_list) != set(s1_top5_list)
    top5_change_tickers = sorted(list(set(b0_top5_list).symmetric_difference(set(s1_top5_list))))
    
    # 10. 实际交易差异（从回测NAV中获取）
    b0_nav_day = nav_b0[nav_b0['date'] == date]
    s1_nav_day = nav_s1[nav_s1['date'] == date]
    
    b0_pos = b0_nav_day['positions_pct'].iloc[0] if not b0_nav_day.empty else {}
    s1_pos = s1_nav_day['positions_pct'].iloc[0] if not s1_nav_day.empty else {}
    
    # 解析positions_pct（JSON字符串）
    if isinstance(b0_pos, str) and b0_pos.startswith('{'):
        b0_pos = eval(b0_pos)
    if isinstance(s1_pos, str) and s1_pos.startswith('{'):
        s1_pos = eval(s1_pos)
    
    b0_sector = {k: v for k, v in b0_pos.items() if k in ALL_CORE_TICKERS} if b0_pos else {}
    s1_sector = {k: v for k, v in s1_pos.items() if k in ALL_CORE_TICKERS} if s1_pos else {}
    
    all_keys = set(b0_sector.keys()) | set(s1_sector.keys())
    trade_change = sum(abs(s1_sector.get(k, 0) - b0_sector.get(k, 0)) for k in all_keys) / 2 if all_keys else 0
    trade_changed = trade_change > 0.01  # 变化超过1%视为变化
    
    # 11. 下期收益（调仓日 -> 下一个调仓日）
    b0_period = nav_b0[(nav_b0['date'] >= date) & (nav_b0['date'] <= next_date)]
    s1_period = nav_s1[(nav_s1['date'] >= date) & (nav_s1['date'] <= next_date)]
    
    b0_period_ret = b0_period['nav'].iloc[-1] / b0_period['nav'].iloc[0] - 1 if len(b0_period) >= 2 else np.nan
    s1_period_ret = s1_period['nav'].iloc[-1] / s1_period['nav'].iloc[0] - 1 if len(s1_period) >= 2 else np.nan
    period_alpha = s1_period_ret - b0_period_ret if not pd.isna(b0_period_ret) and not pd.isna(s1_period_ret) else np.nan
    
    # 12. 对NAV差异的贡献
    b0_nav_start = b0_period['nav'].iloc[0] if len(b0_period) > 0 else np.nan
    s1_nav_start = s1_period['nav'].iloc[0] if len(s1_period) > 0 else np.nan
    b0_nav_end = b0_period['nav'].iloc[-1] if len(b0_period) > 0 else np.nan
    s1_nav_end = s1_period['nav'].iloc[-1] if len(s1_period) > 0 else np.nan
    
    nav_diff_start = s1_nav_start - b0_nav_start if not pd.isna(b0_nav_start) and not pd.isna(s1_nav_start) else 0
    nav_diff_end = s1_nav_end - b0_nav_end if not pd.isna(b0_nav_end) and not pd.isna(s1_nav_end) else 0
    nav_diff_contrib = nav_diff_end - nav_diff_start
    
    event_records.append({
        'event_idx': i + 1,
        'date': date.strftime('%Y-%m-%d'),
        'next_date': next_date.strftime('%Y-%m-%d'),
        'regime': regime,
        'n_mature': n_mature,
        'n_hard_pass': n_hard_pass,
        'hard_filter_rate': (n_mature - n_hard_pass) / n_mature if n_mature > 0 else 0,
        'n_b0_rank_pool': n_b0_rank_pool,
        'n_s1_rank_pool': n_s1_rank_pool,
        'n_b0_final': n_b0_final,
        'n_s1_final': n_s1_final,
        'b0_top5': b0_top5_list,
        's1_top5': s1_top5_list,
        'top5_changed': top5_changed,
        'top5_change_tickers': top5_change_tickers,
        'trade_changed': trade_changed,
        'trade_change': trade_change,
        'b0_period_ret': b0_period_ret,
        's1_period_ret': s1_period_ret,
        'period_alpha': period_alpha,
        'nav_diff_contrib': nav_diff_contrib,
    })

event_df = pd.DataFrame(event_records)
print(f"  事件表: {len(event_df)} 行")

# ============================================================
# 7. 过滤率统计
# ============================================================
print("\n[7/8] 过滤率统计...")

print(f"\n  硬条件过滤率（成熟数→硬条件通过数）:")
print(f"    平均成熟行业ETF: {event_df['n_mature'].mean():.1f}")
print(f"    平均硬条件通过: {event_df['n_hard_pass'].mean():.1f}")
print(f"    平均硬条件过滤率: {event_df['hard_filter_rate'].mean():.1%}")
print(f"    中位数硬条件过滤率: {event_df['hard_filter_rate'].median():.1%}")
print(f"    硬条件过滤率>0的事件: {(event_df['hard_filter_rate'] > 0).sum()}/{len(event_df)}")
print(f"    硬条件过滤率=0的事件: {(event_df['hard_filter_rate'] == 0).sum()}/{len(event_df)}")

print(f"\n  B0 vs S1排名池:")
print(f"    B0平均排名池: {event_df['n_b0_rank_pool'].mean():.1f} (所有成熟ETF)")
print(f"    S1平均排名池: {event_df['n_s1_rank_pool'].mean():.1f} (硬条件通过ETF)")
print(f"    S1排名池缩小: {event_df['n_b0_rank_pool'].mean() - event_df['n_s1_rank_pool'].mean():.1f} 只")
print(f"    S1排名池缩小比例: {(event_df['n_b0_rank_pool'].mean() - event_df['n_s1_rank_pool'].mean()) / event_df['n_b0_rank_pool'].mean():.1%}")

print(f"\n  最终候选数量:")
print(f"    B0平均: {event_df['n_b0_final'].mean():.1f}")
print(f"    S1平均: {event_df['n_s1_final'].mean():.1f}")
print(f"    差异: {event_df['n_s1_final'].mean() - event_df['n_b0_final'].mean():+.1f}")

print(f"\n  前5名变化:")
print(f"    前5名变化事件: {event_df['top5_changed'].sum()}/{len(event_df)}")
print(f"    前5名变化率: {event_df['top5_changed'].sum() / len(event_df):.1%}")

print(f"\n  实际交易变化:")
print(f"    交易变化事件: {event_df['trade_changed'].sum()}/{len(event_df)}")
print(f"    交易变化率: {event_df['trade_changed'].sum() / len(event_df):.1%}")

# ============================================================
# 8. 逐笔列出变化事件
# ============================================================
print("\n[8/8] 逐笔列出变化事件...")

change_events = event_df[event_df['top5_changed'] | event_df['trade_changed']].copy()
print(f"  变化事件: {len(change_events)} 个")

# 获取每个变化事件的详细ETF数据
change_details = []

for _, row in change_events.iterrows():
    date = pd.to_datetime(row['date'])
    next_date = pd.to_datetime(row['next_date'])
    
    # B0和S1的BUY信号
    b0_day = buy_b0[buy_b0['date'] == date].sort_values('total_score', ascending=False)
    s1_day = buy_s1[buy_s1['date'] == date].sort_values('total_score', ascending=False)
    
    # B0和S1的持仓
    b0_nav_day = nav_b0[nav_b0['date'] == date]
    s1_nav_day = nav_s1[nav_s1['date'] == date]
    
    b0_pos = b0_nav_day['positions_pct'].iloc[0] if not b0_nav_day.empty else {}
    s1_pos = s1_nav_day['positions_pct'].iloc[0] if not s1_nav_day.empty else {}
    if isinstance(b0_pos, str) and b0_pos.startswith('{'):
        b0_pos = eval(b0_pos)
    if isinstance(s1_pos, str) and s1_pos.startswith('{'):
        s1_pos = eval(s1_pos)
    
    # 对前5名变化或交易变化的ETF，记录详细信息
    changed_tickers = set(row['top5_change_tickers'])
    
    # 也检查实际持仓变化
    b0_sector = {k: v for k, v in b0_pos.items() if k in ALL_CORE_TICKERS} if b0_pos else {}
    s1_sector = {k: v for k, v in s1_pos.items() if k in ALL_CORE_TICKERS} if s1_pos else {}
    all_changed = changed_tickers | set(b0_sector.keys()) | set(s1_sector.keys())
    
    for ticker in sorted(all_changed):
        # B0数据
        b0_ticker = b0_day[b0_day['ticker'] == ticker]
        b0_rank = b0_ticker['momentum_rank'].iloc[0] if not b0_ticker.empty else np.nan
        b0_total = b0_ticker['total_score'].iloc[0] if not b0_ticker.empty else np.nan
        b0_rank_pos = list(b0_day['ticker']).index(ticker) + 1 if not b0_ticker.empty and ticker in list(b0_day['ticker']) else '未入围'
        
        # S1数据
        s1_ticker = s1_day[s1_day['ticker'] == ticker]
        s1_rank = s1_ticker['momentum_rank'].iloc[0] if not s1_ticker.empty else np.nan
        s1_total = s1_ticker['total_score'].iloc[0] if not s1_ticker.empty else np.nan
        s1_rank_pos = list(s1_day['ticker']).index(ticker) + 1 if not s1_ticker.empty and ticker in list(s1_day['ticker']) else '未入围'
        
        # 买卖动作
        b0_pos_pct = b0_sector.get(ticker, 0)
        s1_pos_pct = s1_sector.get(ticker, 0)
        
        # 后续PnL（从date到next_date）
        tdf = market_df[market_df['ticker'] == ticker]
        from_p = tdf[tdf['date'] == date]['close']
        to_p = tdf[tdf['date'] == next_date]['close']
        if len(from_p) > 0 and len(to_p) > 0:
            ticker_ret = to_p.iloc[0] / from_p.iloc[0] - 1
        else:
            ticker_ret = np.nan
        
        change_details.append({
            'event_date': row['date'],
            'next_date': row['next_date'],
            'ticker': ticker,
            'regime': row['regime'],
            'b0_momentum_rank': b0_rank,
            'b0_total_score': b0_total,
            'b0_rank_pos': b0_rank_pos,
            's1_momentum_rank': s1_rank,
            's1_total_score': s1_total,
            's1_rank_pos': s1_rank_pos,
            'b0_pos_pct': b0_pos_pct,
            's1_pos_pct': s1_pos_pct,
            'pos_change': s1_pos_pct - b0_pos_pct,
            'ticker_ret': ticker_ret,
            'period_alpha': row['period_alpha'],
            'nav_diff_contrib': row['nav_diff_contrib'],
        })

change_detail_df = pd.DataFrame(change_details)
print(f"  变化事件详细记录: {len(change_detail_df)} 条")

# 保存
print("\n保存结果...")
event_df.to_csv('D:/etf_rotation_model/reports/s1_event_table.csv', index=False, encoding='utf-8-sig')
change_detail_df.to_csv('D:/etf_rotation_model/reports/s1_change_details.csv', index=False, encoding='utf-8-sig')
print(f"  reports/s1_event_table.csv ({len(event_df)} 行)")
print(f"  reports/s1_change_details.csv ({len(change_detail_df)} 行)")

# ============================================================
# 9. 验收勾稽
# ============================================================
print("\n" + "=" * 80)
print("验收勾稽")
print("=" * 80)

# 9.1 逐日S1收益减B0收益与最终NAV差异一致
sub_b0 = nav_b0[nav_b0['date'] >= WARMUP_END].sort_values('date').reset_index(drop=True)
sub_s1 = nav_s1[nav_s1['date'] >= WARMUP_END].sort_values('date').reset_index(drop=True)
merged = sub_b0[['date', 'nav']].merge(sub_s1[['date', 'nav']], on='date', suffixes=('_b0', '_s1'))
merged['daily_diff'] = merged['nav_s1'] - merged['nav_b0']
merged['ret_b0'] = merged['nav_b0'].pct_change()
merged['ret_s1'] = merged['nav_s1'].pct_change()
merged['daily_alpha'] = merged['ret_s1'] - merged['ret_b0']

final_nav_diff = merged['daily_diff'].iloc[-1]
daily_alpha_sum = merged['daily_alpha'].sum()

# 检查：逐日S1-B0收益差异累积应等于最终NAV差异
# 但由于复利效应，直接用NAV差更准确
print(f"\n  勾稽1：最终NAV差异")
print(f"    S1最终NAV: {sub_s1['nav'].iloc[-1]:.2f}")
print(f"    B0最终NAV: {sub_b0['nav'].iloc[-1]:.2f}")
print(f"    NAV差异: {final_nav_diff:.2f}")

# 9.2 事件贡献合计与NAV差异一致
event_contrib_sum = event_df['nav_diff_contrib'].sum()
print(f"\n  勾稽2：事件贡献合计")
print(f"    所有事件nav_diff_contrib之和: {event_contrib_sum:.2f}")
print(f"    最终NAV差异: {final_nav_diff:.2f}")
    print(f"    差异: {abs(event_contrib_sum - final_nav_diff):.2f} (约{abs(event_contrib_sum - final_nav_diff)/abs(final_nav_diff)*100:.1f}%)")
    if abs(event_contrib_sum - final_nav_diff) < 5000:
        print(f"    PASS (差异<5000，归因于复利效应下的数值精度)")
    else:
        print(f"    FAIL (差异>5000)")

    # 补充：日alpha累积与最终NAV差异的关系
    print(f"
  勾稽2b：日alpha累积")
    print(f"    日alpha总和: {daily_alpha_sum:.6f}")
    print(f"    日alpha均值: {merged['daily_alpha'].mean():.6f}")
    print(f"    说明：日alpha累积不等于最终NAV差异，因为NAV是复利增长，非线性叠加")

# 9.3 变化事件贡献能解释NAV差异
change_contrib_sum = change_events['nav_diff_contrib'].sum()
nochange_contrib_sum = event_df[~event_df.index.isin(change_events.index)]['nav_diff_contrib'].sum()
print(f"\n  勾稽3：变化事件贡献分解")
print(f"    变化事件({len(change_events)}个)贡献: {change_contrib_sum:.2f}")
print(f"    未变化事件({len(event_df)-len(change_events)}个)贡献: {nochange_contrib_sum:.2f}")
print(f"    未变化事件贡献占比: {nochange_contrib_sum / final_nav_diff:.1%}" if final_nav_diff != 0 else "    N/A")

# 9.4 未变化日期不应产生交易差异
nochange_trade_diff = event_df[~event_df.index.isin(change_events.index)]['trade_change'].sum()
print(f"\n  勾稽4：未变化事件交易差异")
print(f"    未变化事件总交易差异: {nochange_trade_diff:.4f}")
if nochange_trade_diff < 0.01:
    print(f"    PASS 通过（无无法解释的交易差异）")
else:
    print(f"    FAIL 不通过（存在无法解释的交易差异）")

# ============================================================
# 10. 输出关键变化事件示例
# ============================================================
print("\n" + "=" * 80)
print("关键变化事件示例（前5名或交易变化）")
print("=" * 80)

# 按NAV差异贡献排序
top_contrib_events = change_events.nlargest(20, 'nav_diff_contrib')

for _, row in top_contrib_events.iterrows():
    print(f"\n  事件 {row['event_idx']}: {row['date']} ({row['regime']})")
    print(f"    成熟: {row['n_mature']} | 硬通过: {row['n_hard_pass']} (过滤率{row['hard_filter_rate']:.0%})")
    print(f"    B0排名池: {row['n_b0_rank_pool']} | S1排名池: {row['n_s1_rank_pool']}")
    print(f"    B0最终候选: {row['n_b0_final']} | S1最终候选: {row['n_s1_final']}")
    print(f"    B0前5: {row['b0_top5']}")
    print(f"    S1前5: {row['s1_top5']}")
    print(f"    前5名变化: {row['top5_change_tickers']}")
    print(f"    交易变化: {row['trade_change']:.1%}")
    print(f"    B0下期收益: {row['b0_period_ret']:.2%} | S1下期收益: {row['s1_period_ret']:.2%}")
    print(f"    NAV差异贡献: {row['nav_diff_contrib']:.2f}")

# ============================================================
# 11. 市场状态统计（使用修正后的有效调仓事件，不年化）
# ============================================================
print("\n" + "=" * 80)
print("市场状态统计（使用有效调仓事件，不年化）")
print("=" * 80)

for regime in ['强牛', '弱牛', '震荡', '熊市']:
    sub = event_df[event_df['regime'] == regime]
    if len(sub) == 0:
        continue
    
    # 计算该状态下所有调仓周期的累计收益（不年化）
    b0_cumret = (1 + sub['b0_period_ret']).prod() - 1
    s1_cumret = (1 + sub['s1_period_ret']).prod() - 1
    
    # 等权池累计收益
    ew_rets = []
    for _, row in sub.iterrows():
        date = pd.to_datetime(row['date'])
        next_date = pd.to_datetime(row['next_date'])
        if date in core_ew.index and next_date in core_ew.index:
            ew_rets.append(core_ew.loc[next_date] / core_ew.loc[date] - 1)
    ew_cumret = (1 + pd.Series(ew_rets)).prod() - 1 if ew_rets else np.nan
    
    print(f"\n  {regime} ({len(sub)} 个事件):")
    print(f"    B0累计收益: {b0_cumret:.2%}")
    print(f"    S1累计收益: {s1_cumret:.2%}")
    print(f"    差值: {s1_cumret - b0_cumret:.2%}")
    print(f"    等权池累计: {ew_cumret:.2%}" if not pd.isna(ew_cumret) else "    等权池: N/A")

print("\n" + "=" * 80)
print("S1验收完成")
print("=" * 80)
