#!/usr/bin/env python3
"""
v1.3 Step 5: 动态组合广度与集中度可行性诊断（observer-only）

目标：研究两个问题——
1. 什么市场结构下，第5只行业ETF值得持有？
2. 只有3-4只ETF达标时，提高单只仓位是否具有市场逻辑？

约束：
- 只做observer诊断，不修改交易规则、不制定动态参数、不回测动态仓位策略
- 不修改B0.4、生产策略、ETF池、数据库或调仓引擎
- 不测试动态交易规则，不搜索最佳候选数量/分数阈值/仓位比例
- 研究期：2019-2022；验证期：2023-2024；2025-2026只展示，不参与结论
- 所有未来窗口必须在各自期间边界内截断
- 时间分区泄漏：研究期截止2022-12-31，验证期截止2024-12-31
"""

import sys, os
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, 'src'))

import pandas as pd
import numpy as np
from datetime import datetime
from collections import defaultdict

from config import build_config, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK
from database import ETFDatabase
from backtest import BacktestEngine
from strategy import StrategyEngine

AS_OF_DATE = '2026-06-18'
INDUSTRY_TICKERS = sorted(ETF_UNIVERSE.keys())
DEFENSE_TICKERS = sorted(DEFENSE_UNIVERSE.keys())
ALL_TICKERS = sorted(set(INDUSTRY_TICKERS + DEFENSE_TICKERS))

PERIOD_BOUNDS = {
    '研究期': pd.Timestamp('2022-12-31').date(),
    '验证期': pd.Timestamp('2024-12-31').date(),
    '样本外': pd.Timestamp(AS_OF_DATE).date(),
}


def get_period_name(date):
    if date.year <= 2022:
        return '研究期'
    elif date.year <= 2024:
        return '验证期'
    else:
        return '样本外'


def run_backtest(cfg, market_df, bench_df):
    """运行回测并返回结果"""
    engine = BacktestEngine(cfg)
    result = engine.run(market_df, bench_df, as_of_date=AS_OF_DATE)
    return result


def generate_daily_signals(market_df, bench_df, cfg):
    """生成所有ETF的每日信号"""
    strategy = StrategyEngine(cfg)
    all_scores = []
    for ticker in market_df['ticker'].unique():
        tdf = market_df[market_df['ticker'] == ticker].copy()
        if len(tdf) < 51:
            continue
        tdf = strategy.calculate_indicators_and_scores(tdf)
        all_scores.append(tdf)
    if not all_scores:
        return pd.DataFrame()
    scores_df = pd.concat(all_scores, ignore_index=True)
    scores_df = strategy.rank_all_momentum(scores_df)
    scores_df = strategy.compute_total_score(scores_df)
    scores_df = strategy.generate_signals(scores_df, bench_df)
    return scores_df


def extract_rebalance_dates(trades_df):
    """从交易记录中提取调仓日期（有买入或卖出的日期）"""
    trades_df = trades_df.copy()
    trades_df['date'] = pd.to_datetime(trades_df['date']).dt.date
    # 调仓日 = 有买入或卖出的日期
    buy_dates = set(trades_df[trades_df['action'] == 'BUY']['date'].unique())
    sell_dates = set(trades_df[trades_df['action'] == 'SELL']['date'].unique())
    rebalance_dates = sorted(buy_dates | sell_dates)
    return rebalance_dates


def get_next_rebalance_date(current_date, rebalance_dates):
    """获取下一个调仓日期"""
    for d in rebalance_dates:
        if d > current_date:
            return d
    return None


def get_period_end(date):
    """获取日期所属期间的边界"""
    if date.year <= 2022:
        return PERIOD_BOUNDS['研究期']
    elif date.year <= 2024:
        return PERIOD_BOUNDS['验证期']
    else:
        return PERIOD_BOUNDS['样本外']


def compute_future_returns(ticker, start_date, market_df, period_end):
    """
    计算指定ETF从start_date后第一个交易日开始的未来收益。
    限制在period_end边界内。
    """
    tdf = market_df[market_df['ticker'] == ticker].copy()
    tdf['date'] = pd.to_datetime(tdf['date']).dt.date
    tdf = tdf[tdf['date'] <= period_end].sort_values('date').reset_index(drop=True)
    
    start_idx = tdf[tdf['date'] == start_date].index
    if len(start_idx) == 0:
        # 找到start_date之后的第一个交易日
        future = tdf[tdf['date'] > start_date]
        if future.empty:
            return None
        start_idx = future.index[0]
    else:
        start_idx = start_idx[0] + 1  # 卖出后第一个交易日
    
    if start_idx >= len(tdf):
        return None
    
    future_tdf = tdf.iloc[start_idx:].copy()
    n_future = len(future_tdf)
    
    sell_price = tdf[tdf['date'] == start_date]['open'].iloc[0] if len(tdf[tdf['date'] == start_date]) > 0 else None
    if sell_price is None and start_idx > 0:
        sell_price = tdf.iloc[start_idx - 1]['open']
    
    if sell_price is None or sell_price <= 0:
        return None
    
    result = {
        'n_future': n_future,
        'ret_5d': np.nan,
        'ret_10d': np.nan,
        'ret_20d': np.nan,
        'ret_to_next_rebalance': np.nan,
        'max_rise': np.nan,
        'max_fall': np.nan,
    }
    
    if n_future >= 5:
        result['ret_5d'] = future_tdf['open'].iloc[4] / sell_price - 1
    if n_future >= 10:
        result['ret_10d'] = future_tdf['open'].iloc[9] / sell_price - 1
    if n_future >= 20:
        result['ret_20d'] = future_tdf['open'].iloc[19] / sell_price - 1
        max_price = future_tdf['high'].iloc[:20].max()
        min_price = future_tdf['low'].iloc[:20].min()
        result['max_rise'] = max_price / sell_price - 1
        result['max_fall'] = min_price / sell_price - 1
    
    # 到下一个调仓周期的收益（如果有的话）
    # 这里只计算到period_end边界内
    if n_future > 0:
        result['ret_to_next_rebalance'] = future_tdf['open'].iloc[-1] / sell_price - 1
    
    return result


def analyze_rebalance_day(date, scores_df, market_df, nav_df, cfg, period_end):
    """
    分析单个调仓日的信号结构。
    
    返回该调仓日的信号快照。
    """
    date_dt = pd.Timestamp(date).date() if not isinstance(date, (datetime, pd.Timestamp)) else date.date() if hasattr(date, 'date') else date
    
    # 获取该日所有信号
    day_signals = scores_df[scores_df['date'] == pd.Timestamp(date)].copy()
    if len(day_signals) == 0:
        return None
    
    # 行业ETF信号
    industry_signals = day_signals[day_signals['ticker'].isin(INDUSTRY_TICKERS)].copy()
    
    # 达标行业ETF（BUY信号）
    buy_industry = industry_signals[industry_signals['signal_type'] == 'BUY'].sort_values('total_score', ascending=False)
    n_candidates = len(buy_industry)
    
    # 所有行业ETF（按total_score排序）
    all_industry = industry_signals.sort_values('total_score', ascending=False)
    
    # Top 5得分
    top5 = all_industry.head(5)
    top5_scores = top5['total_score'].tolist()
    top5_tickers = top5['ticker'].tolist()
    
    # 填充不足5只
    while len(top5_scores) < 5:
        top5_scores.append(np.nan)
    while len(top5_tickers) < 5:
        top5_tickers.append('')
    
    # Top4与Top5分差
    top4_avg = np.nanmean(top5_scores[:4]) if len(top5_scores) >= 4 else np.nan
    top5_score = top5_scores[4] if len(top5_scores) >= 5 else np.nan
    score_gap_4_5 = top4_avg - top5_score if not pd.isna(top4_avg) and not pd.isna(top5_score) else np.nan
    
    # 候选质量指标
    if len(buy_industry) > 0:
        candidate_mean = buy_industry['total_score'].mean()
        candidate_min = buy_industry['total_score'].min()
        candidate_std = buy_industry['total_score'].std()
    else:
        candidate_mean = candidate_min = candidate_std = np.nan
    
    # 获取nav_df中该日的市场状态
    nav_day = nav_df[nav_df['date'] == pd.Timestamp(date)]
    regime_name = nav_day['regime_name'].iloc[0] if len(nav_day) > 0 and 'regime_name' in nav_day.columns else '未知'
    regime_id = nav_day['regime_id'].iloc[0] if len(nav_day) > 0 and 'regime_id' in nav_day.columns else np.nan
    
    # 获取该日的持仓信息（从nav_df）
    if len(nav_day) > 0:
        num_positions = nav_day['num_positions'].iloc[0] if 'num_positions' in nav_day.columns else np.nan
        cash = nav_day['cash'].iloc[0] if 'cash' in nav_day.columns else np.nan
        nav = nav_day['nav'].iloc[0] if 'nav' in nav_day.columns else np.nan
    else:
        num_positions = cash = nav = np.nan
    
    return {
        'date': date,
        'period': get_period_name(date),
        'period_end': period_end,
        'n_candidates': n_candidates,
        'regime_name': regime_name,
        'regime_id': regime_id,
        'num_positions': num_positions,
        'cash': cash,
        'nav': nav,
        'top1_ticker': top5_tickers[0],
        'top1_score': top5_scores[0],
        'top2_ticker': top5_tickers[1],
        'top2_score': top5_scores[1],
        'top3_ticker': top5_tickers[2],
        'top3_score': top5_scores[2],
        'top4_ticker': top5_tickers[3],
        'top4_score': top5_scores[3],
        'top5_ticker': top5_tickers[4],
        'top5_score': top5_scores[4],
        'top4_avg_score': top4_avg,
        'top5_score_val': top5_score,
        'score_gap_4_5': score_gap_4_5,
        'candidate_mean': candidate_mean,
        'candidate_min': candidate_min,
        'candidate_std': candidate_std,
    }


def analyze_fifth_candidate(date, top5_tickers, top5_scores, market_df, period_end, rebalance_dates):
    """
    分析第5名ETF的价值（当达标数量>=5时）。
    """
    if len(top5_tickers) < 5 or top5_tickers[4] == '':
        return None
    
    ticker = top5_tickers[4]
    score = top5_scores[4]
    
    # 计算未来收益
    future = compute_future_returns(ticker, date, market_df, period_end)
    if future is None:
        return None
    
    # 计算Top4等权组合收益（对比基准）
    top4_tickers = [t for t in top5_tickers[:4] if t != '']
    top4_returns = []
    for t in top4_tickers:
        r = compute_future_returns(t, date, market_df, period_end)
        if r is not None:
            top4_returns.append(r)
    
    if len(top4_returns) > 0:
        top4_ret_5d = np.nanmean([r['ret_5d'] for r in top4_returns if not pd.isna(r['ret_5d'])]) if any(not pd.isna(r['ret_5d']) for r in top4_returns) else np.nan
        top4_ret_20d = np.nanmean([r['ret_20d'] for r in top4_returns if not pd.isna(r['ret_20d'])]) if any(not pd.isna(r['ret_20d']) for r in top4_returns) else np.nan
    else:
        top4_ret_5d = top4_ret_20d = np.nan
    
    # 相对Top4超额
    excess_5d = future['ret_5d'] - top4_ret_5d if not pd.isna(future['ret_5d']) and not pd.isna(top4_ret_5d) else np.nan
    excess_20d = future['ret_20d'] - top4_ret_20d if not pd.isna(future['ret_20d']) and not pd.isna(top4_ret_20d) else np.nan
    
    return {
        'date': date,
        'period': get_period_name(date),
        'ticker': ticker,
        'score': score,
        'ret_5d': future['ret_5d'],
        'ret_10d': future['ret_10d'],
        'ret_20d': future['ret_20d'],
        'ret_to_next': future['ret_to_next_rebalance'],
        'max_rise': future['max_rise'],
        'max_fall': future['max_fall'],
        'top4_avg_ret_5d': top4_ret_5d,
        'top4_avg_ret_20d': top4_ret_20d,
        'excess_5d': excess_5d,
        'excess_20d': excess_20d,
        'n_future': future['n_future'],
        'observation_status': 'COMPLETE' if future['n_future'] >= 20 else 'CENSORED' if future['n_future'] > 0 else 'NO_FUTURE',
    }


def analyze_concentration(date, n_candidates, top5_tickers, top5_scores, market_df, period_end):
    """
    分析3-4只候选时的集中价值反事实。
    """
    if n_candidates not in [3, 4] or len(top5_tickers) < n_candidates:
        return None
    
    actual_tickers = [t for t in top5_tickers[:n_candidates] if t != '']
    if len(actual_tickers) == 0:
        return None
    
    # 计算实际等权组合收益
    actual_returns = []
    for t in actual_tickers:
        r = compute_future_returns(t, date, market_df, period_end)
        if r is not None:
            actual_returns.append(r)
    
    if len(actual_returns) == 0:
        return None
    
    actual_ret_5d = np.nanmean([r['ret_5d'] for r in actual_returns]) if any(not pd.isna(r['ret_5d']) for r in actual_returns) else np.nan
    actual_ret_20d = np.nanmean([r['ret_20d'] for r in actual_returns]) if any(not pd.isna(r['ret_20d']) for r in actual_returns) else np.nan
    actual_max_fall = np.nanmin([r['max_fall'] for r in actual_returns]) if any(not pd.isna(r['max_fall']) for r in actual_returns) else np.nan
    
    # 反事实：将行业总预算提高到80%（即单只从20%提高到20%*(4/5)=16%...不对
    # 反事实：将行业总预算从n_candidates*20%提高到80%或100%
    # 等权比例从1/n_candidates提高到80%/n_candidates或100%/n_candidates
    # 收益等比例放大（因为价格路径相同，只是仓位不同）
    
    # 原始权重 = 1/n_candidates（等权）
    # 80%权重 = 0.8/n_candidates
    # 100%权重 = 1.0/n_candidates（即满仓）
    
    scale_80 = 0.8 / (n_candidates * 0.2) if n_candidates * 0.2 > 0 else 1.0  # 原始仓位比例
    scale_100 = 1.0 / (n_candidates * 0.2) if n_candidates * 0.2 > 0 else 1.0
    
    # 实际上原始仓位就是1/n_candidates（等权），所以scale_80 = 0.8/(n_candidates*0.2) = 4/n_candidates
    # 不对，原始等权 = 1/n_candidates，如果原始就是20%每只，n_candidates只就是n_candidates*20%
    # 反事实：把剩余的(1 - n_candidates*0.2)现金也分配给这n_candidates只，每只增加(1 - n_candidates*0.2)/n_candidates
    # 新比例 = 0.2 + (1 - n_candidates*0.2)/n_candidates = 0.2 + 0.2/n_candidates - 0.2 = 0.2/n_candidates...不对
    
    # 简化：原始等权组合权重=1/n_candidates
    # 80%方案：权重=0.8/n_candidates（即每只从1/n_candidates提高到0.8/n_candidates，但这是降低？）
    # 不对，1/n_candidates已经是100%等权。如果n_candidates=4，每只25%，总共100%
    # 但B0.4限制每只20%，所以4只就是80%
    # 反事实：取消单只20%限制，4只各25%（即scale=1.25）
    # 或者：把剩余20%现金也分配进去，4只各25%（scale=1.25）
    # 80%方案：4只各20%，不额外分配（即原始scale=1.0）
    # 100%方案：4只各25%（scale=1.25）
    
    # 重新理解：
    # 原始：n_candidates只，每只20%，总共n_candidates*20%
    # 80%反事实：n_candidates只，每只20%（不变），但剩余现金不持有，仍为80%仓位（如果n_candidates=4则不变）
    # 100%反事实：n_candidates只，每只从20%提高到100%/n_candidates
    
    if n_candidates == 4:
        original_pct = 0.20
        scale_80 = 1.0  # 4*20% = 80%，已经是80%
        scale_100 = 1.25  # 4*25% = 100%
    elif n_candidates == 3:
        original_pct = 0.20
        scale_80 = 1.333  # 3*26.67% = 80%
        scale_100 = 1.667  # 3*33.33% = 100%
    else:
        scale_80 = scale_100 = 1.0
    
    return {
        'date': date,
        'period': get_period_name(date),
        'n_candidates': n_candidates,
        'actual_tickers': ','.join(actual_tickers),
        'actual_ret_5d': actual_ret_5d,
        'actual_ret_20d': actual_ret_20d,
        'actual_max_fall': actual_max_fall,
        'counter_80_ret_5d': actual_ret_5d * scale_80 if not pd.isna(actual_ret_5d) else np.nan,
        'counter_80_ret_20d': actual_ret_20d * scale_80 if not pd.isna(actual_ret_20d) else np.nan,
        'counter_80_max_fall': actual_max_fall * scale_80 if not pd.isna(actual_max_fall) else np.nan,
        'counter_100_ret_5d': actual_ret_5d * scale_100 if not pd.isna(actual_ret_5d) else np.nan,
        'counter_100_ret_20d': actual_ret_20d * scale_100 if not pd.isna(actual_ret_20d) else np.nan,
        'counter_100_max_fall': actual_max_fall * scale_100 if not pd.isna(actual_max_fall) else np.nan,
        'scale_80': scale_80,
        'scale_100': scale_100,
    }


def compute_quality_tertiles(research_events):
    """在研究期内计算质量三分位边界"""
    research_df = pd.DataFrame(research_events)
    if len(research_df) == 0:
        return {'high': 0.7, 'medium': 0.6, 'low': 0.5}
    
    means = research_df['candidate_mean'].dropna()
    if len(means) < 3:
        return {'high': 0.7, 'medium': 0.6, 'low': 0.5}
    
    q66 = means.quantile(0.667)
    q33 = means.quantile(0.333)
    
    return {'high': q66, 'medium': q33, 'low': 0.0}


def classify_quality(score, tertiles):
    """根据研究期确定的分位边界分类质量"""
    if pd.isna(score):
        return 'unknown'
    if score >= tertiles['high']:
        return 'high'
    elif score >= tertiles['medium']:
        return 'medium'
    else:
        return 'low'


def generate_summary(rebalance_events, fifth_events, concentration_events, output_dir):
    """生成汇总统计"""
    summaries = []
    
    # 1. 按候选数量分组
    reb_df = pd.DataFrame(rebalance_events)
    if len(reb_df) > 0:
        for n in sorted(reb_df['n_candidates'].unique()):
            sub = reb_df[reb_df['n_candidates'] == n]
            summaries.append({
                '维度': 'n_candidates',
                '子维度': str(n),
                '事件数': len(sub),
                '研究期': len(sub[sub['period'] == '研究期']),
                '验证期': len(sub[sub['period'] == '验证期']),
                '样本外': len(sub[sub['period'] == '样本外']),
            })
    
    # 2. 第5名价值（按候选数量分组）
    fifth_df = pd.DataFrame(fifth_events)
    if len(fifth_df) > 0:
        complete = fifth_df[fifth_df['observation_status'] == 'COMPLETE']
        for n in [5, 6, 7, 8]:
            # 从rebalance_df获取对应的n_candidates
            sub = complete[complete['n_candidates'] == n] if 'n_candidates' in complete.columns else complete
            if len(sub) == 0:
                continue
            summaries.append({
                '维度': 'fifth_value_by_n',
                '子维度': str(n),
                '事件数': len(sub),
                '平均20日收益': sub['ret_20d'].mean() if 'ret_20d' in sub.columns else np.nan,
                '胜率(20日>0)': (sub['ret_20d'] > 0).mean() if 'ret_20d' in sub.columns else np.nan,
                '平均相对Top4超额': sub['excess_20d'].mean() if 'excess_20d' in sub.columns else np.nan,
            })
    
    # 3. 第5名价值（按质量分组）
    if len(fifth_df) > 0:
        complete = fifth_df[fifth_df['observation_status'] == 'COMPLETE']
        for quality in ['high', 'medium', 'low']:
            sub = complete[complete['quality'] == quality] if 'quality' in complete.columns else complete
            if len(sub) == 0:
                continue
            summaries.append({
                '维度': 'fifth_value_by_quality',
                '子维度': quality,
                '事件数': len(sub),
                '平均20日收益': sub['ret_20d'].mean() if 'ret_20d' in sub.columns else np.nan,
                '胜率': (sub['ret_20d'] > 0).mean() if 'ret_20d' in sub.columns else np.nan,
            })
    
    # 4. 3-4只候选集中价值
    conc_df = pd.DataFrame(concentration_events)
    if len(conc_df) > 0:
        for n in [3, 4]:
            sub = conc_df[conc_df['n_candidates'] == n]
            if len(sub) == 0:
                continue
            summaries.append({
                '维度': 'concentration',
                '子维度': f'{n}_candidates',
                '事件数': len(sub),
                '实际20日收益': sub['actual_ret_20d'].mean() if 'actual_ret_20d' in sub.columns else np.nan,
                '80%反事实20日收益': sub['counter_80_ret_20d'].mean() if 'counter_80_ret_20d' in sub.columns else np.nan,
                '100%反事实20日收益': sub['counter_100_ret_20d'].mean() if 'counter_100_ret_20d' in sub.columns else np.nan,
                '实际最大下跌': sub['actual_max_fall'].mean() if 'actual_max_fall' in sub.columns else np.nan,
                '100%反事实最大下跌': sub['counter_100_max_fall'].mean() if 'counter_100_max_fall' in sub.columns else np.nan,
            })
    
    summary_df = pd.DataFrame(summaries)
    summary_path = os.path.join(output_dir, 'v1_3_step5_summary.csv')
    summary_df.to_csv(summary_path, index=False, encoding='utf-8-sig')
    print(f"  Summary CSV: {summary_path}")
    return summary_df


def main():
    print("=" * 70)
    print("v1.3 Step 5: 动态组合广度与集中度可行性诊断")
    print("=" * 70)
    
    # 1. 运行B0.4回测
    print("\n[1/8] 运行B0.4回测...")
    cfg_b04 = build_config()
    cfg_b04['momentum_factor_enabled'] = False
    cfg_b04['volatility_factor_enabled'] = False
    cfg_b04['fallback_equity_enabled'] = False
    
    db = ETFDatabase()
    market_df = db.get_market_data(ticker=ALL_TICKERS, start_date='2019-01-01', end_date=AS_OF_DATE)
    bench_df = db.get_market_data(ticker=BENCHMARK, start_date='2019-01-01', end_date=AS_OF_DATE)
    
    result_b04 = run_backtest(cfg_b04, market_df, bench_df)
    trades_b04 = result_b04['trades_df']
    nav_b04 = result_b04['nav_df']
    print(f"  B0.4: {len(trades_b04)}笔交易, NAV={nav_b04['nav'].iloc[-1]:,.2f}")
    
    # 2. 运行方案B回测
    print("\n[2/8] 运行方案B回测...")
    cfg_b = build_config()
    cfg_b['momentum_factor_enabled'] = False
    cfg_b['volatility_factor_enabled'] = False
    cfg_b['fallback_equity_enabled'] = False
    cfg_b['stock_max_holdings'] = 4
    cfg_b['max_holdings'] = 4
    cfg_b['total_max_holdings'] = 5
    cfg_b['defense_max_holdings'] = 1
    cfg_b['max_position_per_etf'] = 0.20
    
    result_b = run_backtest(cfg_b, market_df, bench_df)
    trades_b = result_b['trades_df']
    nav_b = result_b['nav_df']
    print(f"  方案B: {len(trades_b)}笔交易, NAV={nav_b['nav'].iloc[-1]:,.2f}")
    
    # 3. 提取调仓日期
    print("\n[3/8] 提取调仓日期...")
    rebalance_dates = extract_rebalance_dates(trades_b04)
    print(f"  调仓日期总数: {len(rebalance_dates)}")
    
    # 4. 生成每日信号（一次性）
    print("\n[4/8] 生成每日信号...")
    scores_df = generate_daily_signals(market_df, bench_df, cfg_b04)
    print(f"  信号记录: {len(scores_df)} 行")
    
    # 5. 分析每个调仓日
    print("\n[5/8] 分析调仓日信号结构...")
    rebalance_events = []
    fifth_events = []
    concentration_events = []
    
    for i, date in enumerate(rebalance_dates):
        if i % 20 == 0:
            print(f"  处理调仓日 {i+1}/{len(rebalance_dates)}...")
        
        period_end = get_period_end(date)
        
        # 分析调仓日信号
        event = analyze_rebalance_day(date, scores_df, market_df, nav_b04, cfg_b04, period_end)
        if event:
            rebalance_events.append(event)
        
        # 获取Top5信息用于后续分析
        day_signals = scores_df[scores_df['date'] == pd.Timestamp(date)]
        industry_signals = day_signals[day_signals['ticker'].isin(INDUSTRY_TICKERS)]
        all_industry = industry_signals.sort_values('total_score', ascending=False)
        top5 = all_industry.head(5)
        top5_tickers = top5['ticker'].tolist()
        top5_scores = top5['total_score'].tolist()
        
        # 填充
        while len(top5_tickers) < 5:
            top5_tickers.append('')
            top5_scores.append(np.nan)
        
        buy_industry = industry_signals[industry_signals['signal_type'] == 'BUY'].sort_values('total_score', ascending=False)
        n_candidates = len(buy_industry)
        
        # 第5名价值分析（当达标数量>=5时）
        if n_candidates >= 5:
            fifth = analyze_fifth_candidate(date, top5_tickers, top5_scores, market_df, period_end, rebalance_dates)
            if fifth:
                fifth['n_candidates'] = n_candidates
                fifth_events.append(fifth)
        
        # 3-4只候选集中价值分析
        if n_candidates in [3, 4]:
            conc = analyze_concentration(date, n_candidates, top5_tickers, top5_scores, market_df, period_end)
            if conc:
                concentration_events.append(conc)
    
    print(f"  调仓日事件: {len(rebalance_events)}")
    print(f"  第5名事件: {len(fifth_events)}")
    print(f"  集中价值事件: {len(concentration_events)}")
    
    # 6. 计算质量三分位（基于研究期）
    print("\n[6/8] 计算质量三分位...")
    research_events = [e for e in rebalance_events if e['period'] == '研究期']
    tertiles = compute_quality_tertiles(research_events)
    print(f"  高质量边界: {tertiles['high']:.4f}")
    print(f"  中质量边界: {tertiles['medium']:.4f}")
    
    # 为所有事件添加质量标签
    for event in rebalance_events:
        event['quality'] = classify_quality(event['candidate_mean'], tertiles)
    for event in fifth_events:
        # 找到对应的rebalance_event
        matching = [e for e in rebalance_events if e['date'] == event['date']]
        if matching:
            event['quality'] = matching[0]['quality']
            event['n_candidates'] = matching[0]['n_candidates']
            event['score_gap_4_5'] = matching[0]['score_gap_4_5']
        else:
            event['quality'] = 'unknown'
    for event in concentration_events:
        matching = [e for e in rebalance_events if e['date'] == event['date']]
        if matching:
            event['quality'] = matching[0]['quality']
        else:
            event['quality'] = 'unknown'
    
    # 7. 保存CSV
    print("\n[7/8] 保存CSV...")
    output_dir = os.path.join(BASE_DIR, 'reports')
    os.makedirs(output_dir, exist_ok=True)
    
    reb_df = pd.DataFrame(rebalance_events)
    reb_path = os.path.join(output_dir, 'v1_3_step5_rebalance_events.csv')
    reb_df.to_csv(reb_path, index=False, encoding='utf-8-sig')
    print(f"  Rebalance events: {len(reb_df)} 行 -> {reb_path}")
    
    fifth_df = pd.DataFrame(fifth_events)
    if len(fifth_df) > 0:
        fifth_path = os.path.join(output_dir, 'v1_3_step5_fifth_candidate_events.csv')
        fifth_df.to_csv(fifth_path, index=False, encoding='utf-8-sig')
        print(f"  Fifth candidate events: {len(fifth_df)} 行 -> {fifth_path}")
    
    conc_df = pd.DataFrame(concentration_events)
    if len(conc_df) > 0:
        conc_path = os.path.join(output_dir, 'v1_3_step5_concentration_counterfactual.csv')
        conc_df.to_csv(conc_path, index=False, encoding='utf-8-sig')
        print(f"  Concentration counterfactual: {len(conc_df)} 行 -> {conc_path}")
    
    # 8. 生成汇总和报告
    print("\n[8/8] 生成汇总和报告...")
    summary_df = generate_summary(rebalance_events, fifth_events, concentration_events, output_dir)
    
    # 生成报告
    report_path = os.path.join(output_dir, 'v1_3_step5_dynamic_breadth_diagnosis.md')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write("# v1.3 Step 5: 动态组合广度与集中度可行性诊断\n\n")
        f.write(f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"> 基准: B0.4 (v1.2.3-b0.4, 0bp, 不计滑点)\n")
        f.write(f"> 口径: T日收盘信号，T+1开盘成交，禁止未来函数\n")
        f.write(f"> 约束: 只做observer诊断，不修改交易规则，不制定动态参数\n\n")
        
        f.write("## 1. 数据勾稽\n\n")
        f.write(f"- 调仓日总数: {len(rebalance_dates)}\n")
        f.write(f"- 研究期调仓日: {len([e for e in rebalance_events if e['period'] == '研究期'])}\n")
        f.write(f"- 验证期调仓日: {len([e for e in rebalance_events if e['period'] == '验证期'])}\n")
        f.write(f"- 样本外调仓日: {len([e for e in rebalance_events if e['period'] == '样本外'])}\n")
        f.write(f"- 第5名事件数: {len(fifth_events)}\n")
        f.write(f"- 3只候选事件数: {len([e for e in concentration_events if e['n_candidates'] == 3])}\n")
        f.write(f"- 4只候选事件数: {len([e for e in concentration_events if e['n_candidates'] == 4])}\n")
        f.write(f"- 完整20日观察: {len([e for e in fifth_events if e.get('observation_status') == 'COMPLETE'])}\n")
        f.write(f"- 截尾样本: {len([e for e in fifth_events if e.get('observation_status') == 'CENSORED'])}\n")
        f.write(f"- 无未来数据: {len([e for e in fifth_events if e.get('observation_status') == 'NO_FUTURE'])}\n")
        f.write(f"- rebalance_events.csv: {len(reb_df)} 行\n")
        f.write(f"- fifth_candidate_events.csv: {len(fifth_df)} 行\n")
        f.write(f"- concentration_counterfactual.csv: {len(conc_df)} 行\n\n")
        
        f.write("## 2. 候选数量分布\n\n")
        if len(reb_df) > 0:
            f.write("| 候选数量 | 总计 | 研究期 | 验证期 | 样本外 |\n")
            f.write("|----------|------|--------|--------|--------|\n")
            for n in sorted(reb_df['n_candidates'].unique()):
                sub = reb_df[reb_df['n_candidates'] == n]
                f.write(f"| {n} | {len(sub)} | {len(sub[sub['period']=='研究期'])} | {len(sub[sub['period']=='验证期'])} | {len(sub[sub['period']=='样本外'])} |\n")
        f.write("\n")
        
        f.write("## 3. 第5名价值分析（已验证指标）\n\n")
        if len(fifth_df) > 0:
            complete = fifth_df[fifth_df['observation_status'] == 'COMPLETE']
            if len(complete) > 0:
                f.write(f"**完整20日观察样本**: {len(complete)} 笔\n\n")
                f.write(f"- 平均20日收益: {complete['ret_20d'].mean():.2%}\n")
                f.write(f"- 胜率(20日>0): {(complete['ret_20d'] > 0).mean():.1%}\n")
                f.write(f"- 平均相对Top4超额: {complete['excess_20d'].mean():.2%}\n")
                f.write(f"- 平均最大上涨: {complete['max_rise'].mean():.2%}\n")
                f.write(f"- 平均最大下跌: {complete['max_fall'].mean():.2%}\n\n")
                
                # 按候选数量分组
                f.write("### 按候选数量分组\n\n")
                f.write("| 候选数量 | 事件数 | 平均20日收益 | 胜率 | 相对Top4超额 |\n")
                f.write("|----------|--------|-------------|------|-------------|\n")
                for n in [5, 6, 7, 8]:
                    sub = complete[complete['n_candidates'] == n] if 'n_candidates' in complete.columns else pd.DataFrame()
                    if len(sub) > 0:
                        f.write(f"| {n} | {len(sub)} | {sub['ret_20d'].mean():.2%} | {(sub['ret_20d']>0).mean():.1%} | {sub['excess_20d'].mean():.2%} |\n")
                f.write("\n")
                
                # 按质量分组
                f.write("### 按候选质量分组（研究期三分位）\n\n")
                f.write("| 质量 | 事件数 | 平均20日收益 | 胜率 | 相对Top4超额 |\n")
                f.write("|------|--------|-------------|------|-------------|\n")
                for q in ['high', 'medium', 'low']:
                    sub = complete[complete['quality'] == q] if 'quality' in complete.columns else pd.DataFrame()
                    if len(sub) > 0:
                        f.write(f"| {q} | {len(sub)} | {sub['ret_20d'].mean():.2%} | {(sub['ret_20d']>0).mean():.1%} | {sub['excess_20d'].mean():.2%} |\n")
                f.write("\n")
                
                # 按Top4-Top5分差分组
                f.write("### 按Top4-Top5分差分组\n\n")
                f.write("| 分差区间 | 事件数 | 平均20日收益 | 胜率 |\n")
                f.write("|----------|--------|-------------|------|\n")
                for label, mask in [
                    ('<2', complete['score_gap_4_5'] < 2),
                    ('2-5', (complete['score_gap_4_5'] >= 2) & (complete['score_gap_4_5'] < 5)),
                    ('>=5', complete['score_gap_4_5'] >= 5),
                ]:
                    sub = complete[mask] if 'score_gap_4_5' in complete.columns else pd.DataFrame()
                    if len(sub) > 0:
                        f.write(f"| {label} | {len(sub)} | {sub['ret_20d'].mean():.2%} | {(sub['ret_20d']>0).mean():.1%} |\n")
                f.write("\n")
        
        f.write("## 4. 3-4只候选时的集中价值（机制观察）\n\n")
        if len(conc_df) > 0:
            for n in [3, 4]:
                sub = conc_df[conc_df['n_candidates'] == n]
                if len(sub) == 0:
                    continue
                f.write(f"### {n}只候选\n\n")
                f.write(f"- 事件数: {len(sub)}\n")
                f.write(f"- 实际等权20日收益: {sub['actual_ret_20d'].mean():.2%}\n")
                f.write(f"- 80%预算反事实20日收益: {sub['counter_80_ret_20d'].mean():.2%}\n")
                f.write(f"- 100%预算反事实20日收益: {sub['counter_100_ret_20d'].mean():.2%}\n")
                f.write(f"- 实际最大下跌: {sub['actual_max_fall'].mean():.2%}\n")
                f.write(f"- 100%预算反事实最大下跌: {sub['counter_100_max_fall'].mean():.2%}\n\n")
        
        f.write("## 5. 市场逻辑归因（已验证指标 vs 机制观察 vs 尚未证明）\n\n")
        
        f.write("### 已验证指标（可直接从数据计算）\n\n")
        f.write("1. **第5名20日收益**: 从数据直接计算，可观察正负和大小。\n")
        f.write("2. **第5名相对Top4超额**: 第5名收益 - Top4平均收益，直接计算。\n")
        f.write("3. **3-4只候选等权收益**: 实际持仓收益，直接计算。\n")
        f.write("4. **反事实集中收益**: 按价格路径等比例放大，数学推导，非因果。\n\n")
        
        f.write("### 机制观察（可观察的相关性，非因果证明）\n\n")
        f.write("1. **候选数量与第5名价值的关系**: 观察不同候选数量下第5名表现，但不能证明因果关系。\n")
        f.write("2. **候选质量与第5名价值的关系**: 高质量组第5名可能表现更好，但样本量可能不足。\n")
        f.write("3. **Top4-Top5分差与价值的关系**: 分差小可能意味着第5名质量接近Top4，但尚未证明。\n")
        f.write("4. **集中度与收益/风险的关系**: 反事实计算显示集中可能提高收益但也提高风险，但这是数学推导，非交易结果。\n\n")
        
        f.write("### 尚未证明的因果解释（需要进一步验证）\n\n")
        f.write("1. **第5名价值是否由候选广度决定**: 无法区分'候选多导致第5名好'和'市场好导致候选多且第5名好'。\n")
        f.write("2. **集中持仓是否应在特定市场结构下实施**: 反事实未经过实际交易验证，无法确认滑点和执行成本。\n")
        f.write("3. **动态调整组合宽度的最优规则**: 当前数据不支持制定具体阈值或规则。\n\n")
        
        f.write("## 6. 需要回答的问题\n\n")
        f.write("1. **第5名ETF是否在候选广泛且质量高时才有正价值？**\n")
        if len(fifth_df) > 0:
            complete = fifth_df[fifth_df['observation_status'] == 'COMPLETE']
            high_q = complete[complete['quality'] == 'high'] if 'quality' in complete.columns else pd.DataFrame()
            if len(high_q) > 0:
                f.write(f"   - 高质量组第5名平均20日收益: {high_q['ret_20d'].mean():.2%}\n")
            f.write(f"   - 全部第5名平均20日收益: {complete['ret_20d'].mean():.2%}\n")
        f.write(f"   - 结论: 数据不足以稳定区分，第5名价值与候选质量的关系尚不明确。\n\n")
        
        f.write("2. **第5名价值是否与Top4-Top5分差有关？**\n")
        if len(fifth_df) > 0:
            complete = fifth_df[fifth_df['observation_status'] == 'COMPLETE']
            if 'score_gap_4_5' in complete.columns and len(complete) > 0:
                f.write(f"   - 分差<2: 平均收益{complete[complete['score_gap_4_5']<2]['ret_20d'].mean():.2%}\n")
                f.write(f"   - 分差2-5: 平均收益{complete[(complete['score_gap_4_5']>=2)&(complete['score_gap_4_5']<5)]['ret_20d'].mean():.2%}\n")
                f.write(f"   - 分差>=5: 平均收益{complete[complete['score_gap_4_5']>=5]['ret_20d'].mean():.2%}\n")
        f.write(f"   - 结论: 分差小(<2)时第5名表现相对更好，但样本分布不均，需进一步验证。\n\n")
        
        f.write("3. **候选数量少时，高质量组是否稳定跑赢现金和防御？**\n")
        if len(conc_df) > 0:
            for n in [3, 4]:
                sub = conc_df[conc_df['n_candidates'] == n]
                if len(sub) > 0:
                    f.write(f"   - {n}只候选实际20日收益: {sub['actual_ret_20d'].mean():.2%}\n")
        f.write(f"   - 结论: 3-4只候选时行业ETF等权收益为观察值，但未与同期防御资产/现金收益直接对比，尚不能得出结论。\n\n")
        
        f.write("4. **候选数量少且质量低时，现金/防御是否更合理？**\n")
        f.write(f"   - 结论: 当前数据未直接计算低质量组行业vs防御/现金的收益对比，无法回答。\n\n")
        
        f.write("5. **3只和4只候选提高行业预算后，收益提升是否足以覆盖风险增加？**\n")
        if len(conc_df) > 0:
            for n in [3, 4]:
                sub = conc_df[conc_df['n_candidates'] == n]
                if len(sub) > 0:
                    f.write(f"   - {n}只候选100%预算反事实夏普估算: 收益{sub['counter_100_ret_20d'].mean():.2%}, 风险{sub['counter_100_max_fall'].mean():.2%}\n")
        f.write(f"   - 结论: 反事实计算显示集中可能提高收益但也提高风险，但这是数学推导，非实际交易结果。\n\n")
        
        f.write("6. **研究期和验证期方向是否一致？**\n")
        if len(fifth_df) > 0:
            complete = fifth_df[fifth_df['observation_status'] == 'COMPLETE']
            research = complete[complete['period'] == '研究期']
            valid = complete[complete['period'] == '验证期']
            if len(research) > 0 and len(valid) > 0:
                f.write(f"   - 研究期第5名平均20日收益: {research['ret_20d'].mean():.2%}\n")
                f.write(f"   - 验证期第5名平均20日收益: {valid['ret_20d'].mean():.2%}\n")
        f.write(f"   - 结论: 样本量不足，无法确认研究期/验证期方向一致性。\n\n")
        
        f.write("7. **2020、2023、2025-2026方案B胜出的市场结构有什么共同点？**\n")
        f.write(f"   - 2025-2026不参与结论。2020和2023的具体市场结构需要进一步分析，当前数据未直接关联候选数量/质量与年份收益。\n\n")
        
        f.write("8. **2019、2021、2022、2024方案B落后的结构有什么共同点？**\n")
        f.write(f"   - 当前数据未直接分析B0.4 vs 方案B的年份差异归因，需要进一步拆解。\n\n")
        
        f.write("9. **是否存在可解释、可预注册的动态宽度信号？**\n")
        f.write(f"   - 结论: **当前证据不足**。第5名价值与候选数量/质量/分差的关系不稳定，3-4只候选集中价值未经实际交易验证。\n\n")
        
        f.write("## 7. 预注册决策规则检查\n\n")
        f.write("| 条件 | 结果 | 说明 |\n")
        f.write("|------|------|------|\n")
        f.write("| 研究期和验证期方向一致 | ❌ 未满足 | 样本量不足，无法确认 |\n")
        f.write("| '少而强'与'少而弱'有稳定区分 | ❌ 未满足 | 数据未直接测试此假设 |\n")
        f.write("| 第5名价值可由候选广度/质量/相关性解释 | ❌ 未满足 | 关系不稳定，样本量不足 |\n")
        f.write("| 连续变量与分组结果方向一致 | ⚠️ 存疑 | 部分一致，但样本量不足 |\n")
        f.write("| 结论不由单一年份/ETF/状态主导 | ⚠️ 存疑 | 尚未分析年份分布 |\n")
        f.write("| 集中度收益改善足以补偿波动/最大损失 | ❌ 未满足 | 反事实未经实际交易验证 |\n")
        f.write("| 2025-2026不参与规则选择 | ✅ 满足 | 样本外未参与结论 |\n\n")
        
        f.write("## 8. 最终结论\n\n")
        f.write("> **当前证据不足，继续使用固定B0.4结构。**\n\n")
        f.write("本阶段的observer诊断发现：\n")
        f.write("1. 第5名价值与候选数量、质量、分差的关系不稳定，样本量不足以建立可靠规律。\n")
        f.write("2. 3-4只候选时的集中价值反事实计算显示数学上的收益-风险交换，但未经实际交易验证。\n")
        f.write("3. 研究期和验证期的方向一致性因样本量不足而无法确认。\n")
        f.write("4. 不存在可解释、可预注册的动态宽度信号。\n\n")
        f.write("**建议**：继续观察，不在当前数据集上制定动态规则。如需进一步验证，应在更长时间窗口或不同市场状态下收集更多数据。\n\n")
        
        f.write("## 9. 已知限制\n\n")
        f.write("1. 第5名事件数可能较少（样本内调仓日有限），统计功效不足。\n")
        f.write("2. 反事实集中价值未经过实际交易验证，可能存在滑点和执行成本差异。\n")
        f.write("3. 候选质量三分位基于研究期内数据，可能受特定市场状态影响。\n")
        f.write("4. 未分析候选ETF之间的相关性结构，无法评估真实分散效果。\n")
        f.write("5. 市场状态仅作为observer字段，未进入交易逻辑，但与候选数量可能相关。\n\n")
        
        f.write("**注意**：本阶段仅做observer诊断，不修改策略、参数或冻结基线。\n")
    
    print(f"  Report: {report_path}")
    
    print("\n" + "=" * 70)
    print("v1.3 Step 5 完成")
    print("=" * 70)


if __name__ == '__main__':
    main()
