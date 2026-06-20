#!/usr/bin/env python3
"""Phase 5.8c: B/C差异分析脚本（独立运行，不重新运行完整回测）"""

import sys, os
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, 'src'))

import pandas as pd
import numpy as np
from config import build_config, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK
from database import ETFDatabase
from backtest import BacktestEngine
from strategy import StrategyEngine

TRAIN_END = '2022-12-30'
VALID_END = '2024-12-31'

class SchemeB_HighVol20Pct(StrategyEngine):
    def compute_total_score(self, scores_df, exclude_factor=None):
        df = scores_df.copy()
        result_dfs = []
        for date in df['date'].unique():
            day_df = df[df['date'] == date].copy()
            vols = day_df['volatility_20'].abs()
            if len(vols) >= 2:
                q80 = vols.quantile(0.8)
                q20 = vols.quantile(0.2)
                day_df['vol_score'] = np.where(vols >= q80, 10,
                                      np.where(vols >= q20, 5, 0))
            else:
                day_df['vol_score'] = 0
            result_dfs.append(day_df)
        df = pd.concat(result_dfs, ignore_index=True)
        df['total_score'] = (df['trend_score'].fillna(0) + df['confirm_score'].fillna(0) +
                             df['momentum_rank'].fillna(0) + df['volume_score'].fillna(0) +
                             df['vol_score'].fillna(0))
        if exclude_factor is not None:
            if exclude_factor in df.columns:
                df['total_score'] -= df[exclude_factor].fillna(0)
        return df

class SchemeC_HighVolWithTrend(StrategyEngine):
    def compute_total_score(self, scores_df, exclude_factor=None):
        df = scores_df.copy()
        result_dfs = []
        for date in df['date'].unique():
            day_df = df[df['date'] == date].copy()
            vols = day_df['volatility_20'].abs()
            if len(vols) >= 2:
                q80 = vols.quantile(0.8)
                q20 = vols.quantile(0.2)
                has_trend = day_df['trend_score'] > 0
                day_df['vol_score'] = np.where(has_trend & (vols >= q80), 10,
                                      np.where(has_trend & (vols >= q20), 5, 0))
            else:
                day_df['vol_score'] = 0
            result_dfs.append(day_df)
        df = pd.concat(result_dfs, ignore_index=True)
        df['total_score'] = (df['trend_score'].fillna(0) + df['confirm_score'].fillna(0) +
                             df['momentum_rank'].fillna(0) + df['volume_score'].fillna(0) +
                             df['vol_score'].fillna(0))
        if exclude_factor is not None:
            if exclude_factor in df.columns:
                df['total_score'] -= df[exclude_factor].fillna(0)
        return df


def get_scores(engine, market_df, start_date, end_date):
    all_scores = []
    for ticker in market_df['ticker'].unique():
        ticker_df = market_df[market_df['ticker'] == ticker].copy()
        if len(ticker_df) < 50:
            continue
        ticker_df = engine.calculate_indicators(ticker_df)
        scored = engine.calculate_scores(ticker_df)
        all_scores.append(scored)
    scores_df = pd.concat(all_scores, ignore_index=True)
    scores_df = engine.rank_all_momentum(scores_df)
    scores_df = engine.compute_total_score(scores_df)
    scores_df = scores_df[(scores_df['date'] >= start_date) & (scores_df['date'] <= end_date)]
    return scores_df


def run_backtest(engine_cls, as_of_date, perf_start=None):
    cfg = build_config()
    cfg['fallback_equity_enabled'] = False
    cfg['volatility_factor_enabled'] = True
    db = ETFDatabase()
    tickers = sorted(set(list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())))
    market_df = db.get_market_data(ticker=tickers, start_date='2019-01-01', end_date=as_of_date)
    bench_df = db.get_market_data(ticker=BENCHMARK, start_date='2019-01-01', end_date=as_of_date)
    engine = BacktestEngine(cfg)
    engine.strategy = engine_cls(cfg)
    return engine.run(market_df, bench_df, as_of_date=as_of_date, performance_start=perf_start)


def main():
    print("=== Phase 5.8c: B/C差异分析 ===")
    
    db = ETFDatabase()
    market_df = db.get_market_data(ticker=list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys()),
                                    start_date='2019-01-01', end_date=VALID_END)
    
    cfg = build_config()
    cfg['fallback_equity_enabled'] = False
    cfg['volatility_factor_enabled'] = True
    
    # 1. 检查训练期vol_score差异
    print("\n[1] 训练期 B/C vol_score 差异分析...")
    b_engine = SchemeB_HighVol20Pct(cfg)
    c_engine = SchemeC_HighVolWithTrend(cfg)
    
    b_scores = get_scores(b_engine, market_df, '2019-06-01', TRAIN_END)
    c_scores = get_scores(c_engine, market_df, '2019-06-01', TRAIN_END)
    
    # 合并B/C的vol_score
    merged = b_scores[['date', 'ticker', 'vol_score', 'trend_score']].merge(
        c_scores[['date', 'ticker', 'vol_score']], on=['date', 'ticker'], suffixes=('_B', '_C')
    )
    
    diff_count = (merged['vol_score_B'] != merged['vol_score_C']).sum()
    total_count = len(merged)
    print(f"  总记录数: {total_count}")
    print(f"  vol_score不同的记录: {diff_count} ({diff_count/total_count*100:.1f}%)")
    
    # 当B=10或5时，C是否一定相同？
    b_nonzero = merged[merged['vol_score_B'] > 0]
    c_zero_when_b_nonzero = b_nonzero[b_nonzero['vol_score_C'] == 0]
    print(f"  B非零但C为零的记录: {len(c_zero_when_b_nonzero)} / {len(b_nonzero)} ({len(c_zero_when_b_nonzero)/len(b_nonzero)*100:.1f}%)")
    
    # 检查是否这些差异发生在trend_score<=0的ETF上
    if len(c_zero_when_b_nonzero) > 0:
        trend_nonpos = c_zero_when_b_nonzero[c_zero_when_b_nonzero['trend_score'] <= 0]
        print(f"  其中trend_score<=0的: {len(trend_nonpos)} / {len(c_zero_when_b_nonzero)} ({len(trend_nonpos)/len(c_zero_when_b_nonzero)*100:.1f}%)")
    
    # 2. 检查验证期交易序列差异
    print("\n[2] 验证期 B/C 交易序列差异...")
    b_result = run_backtest(SchemeB_HighVol20Pct, VALID_END, '2023-01-01')
    c_result = run_backtest(SchemeC_HighVolWithTrend, VALID_END, '2023-01-01')
    
    b_trades = b_result['trades_df']
    c_trades = c_result['trades_df']
    
    print(f"  B交易次数: {len(b_trades)}")
    print(f"  C交易次数: {len(c_trades)}")
    
    identical = False
    if len(b_trades) == len(c_trades) and len(b_trades) > 0:
        cols = ['date', 'action', 'ticker', 'shares', 'price']
        avail = [c for c in cols if c in b_trades.columns and c in c_trades.columns]
        b_sorted = b_trades[avail].sort_values(avail).reset_index(drop=True)
        c_sorted = c_trades[avail].sort_values(avail).reset_index(drop=True)
        identical = b_sorted.equals(c_sorted)
        print(f"  交易序列完全相同: {identical}")
        if not identical:
            diff_rows = (b_sorted != c_sorted).any(axis=1).sum()
            print(f"  差异行数: {diff_rows}")
    else:
        print(f"  交易次数不同，无法逐行比较")
    
    # 3. 检查B/C vol_score差异对排名和入选的影响
    print("\n[3] B/C vol_score差异对排名和入选的影响...")
    
    # 找到所有B/C vol_score不同的日期
    diff_dates = merged[merged['vol_score_B'] != merged['vol_score_C']]['date'].unique()
    print(f"  vol_score不同的日期数: {len(diff_dates)} / {merged['date'].nunique()} ({len(diff_dates)/merged['date'].nunique()*100:.1f}%)")
    
    # 计算B/C的total_score差异（momentum_rank=0, volume_score=0简化）
    merged['total_B'] = merged['trend_score'].fillna(0) + merged['vol_score_B']
    merged['total_C'] = merged['trend_score'].fillna(0) + merged['vol_score_C']
    
    # 找到total_score不同的记录
    total_diff = merged[merged['total_B'] != merged['total_C']]
    print(f"  total_score不同的记录: {len(total_diff)} / {total_count} ({len(total_diff)/total_count*100:.1f}%)")
    
    # 检查这些不同的total_score是否改变了排名
    changed_rank = 0
    for date in diff_dates[:100]:  # 取样检查
        day_b = merged[merged['date'] == date].sort_values('total_B', ascending=False).reset_index(drop=True)
        day_c = merged[merged['date'] == date].sort_values('total_C', ascending=False).reset_index(drop=True)
        # 检查前5名是否相同
        if len(day_b) >= 5 and len(day_c) >= 5:
            b_top5 = set(day_b['ticker'].head(5))
            c_top5 = set(day_c['ticker'].head(5))
            if b_top5 != c_top5:
                changed_rank += 1
    
    if len(diff_dates) > 0:
        print(f"  前5名ETF发生变化的日期(取样): {changed_rank} / {min(len(diff_dates), 100)} ({changed_rank/min(len(diff_dates),100)*100:.1f}%)")
    
    print("\n=== 分析完成 ===")
    return {
        'diff_count': int(diff_count),
        'total_count': int(total_count),
        'diff_pct': float(diff_count/total_count*100),
        'b_trades': len(b_trades),
        'c_trades': len(c_trades),
        'identical': bool(identical),
        'diff_dates': int(len(diff_dates)),
    }


if __name__ == '__main__':
    main()
