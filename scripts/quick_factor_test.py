"""
快速因子验证脚本 - 只测试调仓日因子
使用预计算的评分数据，避免重复计算，大幅加速
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pandas as pd
import numpy as np
from datetime import datetime

from config import build_config, FACTOR_CONFIG, BACKTEST_CONFIG, BENCHMARK
from backtest import BacktestEngine
from database import ETFDatabase
from strategy import StrategyEngine


def precompute_scores(market_df, bench_df, sector_df=None):
    """预计算所有ETF的评分和信号（与因子参数无关）"""
    cfg = build_config()
    strategy = StrategyEngine(cfg)
    
    # 计算板块评分
    sector_scores_df = None
    if sector_df is not None and not sector_df.empty and cfg.get('sector_boost_enabled', False):
        print("  计算板块评分...")
        sector_scores_list = []
        for ticker in sector_df['ticker'].unique():
            sector_ticker_df = sector_df[sector_df['ticker'] == ticker].copy()
            if len(sector_ticker_df) < 50:
                continue
            sector_scored = strategy.calculate_sector_total_score(sector_ticker_df)
            sector_scores_list.append(sector_scored)
        
        if sector_scores_list:
            sector_scores_df = pd.concat(sector_scores_list, ignore_index=True)
    
    # 计算ETF评分
    print("  计算ETF评分...")
    all_scores = []
    for ticker in market_df['ticker'].unique():
        ticker_df = market_df[market_df['ticker'] == ticker].copy()
        if len(ticker_df) < 50:
            continue
        scored = strategy.calculate_total_score(ticker_df, sector_scores_df)
        all_scores.append(scored)
    
    scores_df = pd.concat(all_scores, ignore_index=True)
    
    # 生成信号
    print("  生成交易信号...")
    signals_df = strategy.generate_signals(scores_df, bench_df)
    
    return signals_df, scores_df


def test_rebalance_weekday(signals_df, market_df, bench_df, weekday, sample='all'):
    """测试特定调仓日的回测效果"""
    factor_cfg = FACTOR_CONFIG.copy()
    factor_cfg['rebalance_weekday'] = weekday
    factor_cfg['rebalance_freq'] = 'weekly'
    
    cfg = build_config(factor_cfg=factor_cfg)
    engine = BacktestEngine(cfg)
    
    # 过滤区间
    if sample == 'in':
        end = BACKTEST_CONFIG['in_sample_end']
        signals_df = signals_df[signals_df['date'] <= end]
        market_df = market_df[market_df['date'] <= end]
        bench_df = bench_df[bench_df['date'] <= end]
    elif sample == 'out':
        start = BACKTEST_CONFIG['out_sample_start']
        signals_df = signals_df[signals_df['date'] >= start]
        market_df = market_df[market_df['date'] >= start]
        bench_df = bench_df[bench_df['date'] >= start]
    
    result = engine._execute_backtest(signals_df, market_df, bench_df)
    
    return {
        'weekday': weekday,
        'weekday_name': ['周一', '周二', '周三', '周四', '周五'][weekday],
        'total_return': result['total_return'],
        'annual_return': result['annual_return'],
        'sharpe_ratio': result['sharpe_ratio'],
        'max_drawdown': result['max_drawdown'],
        'num_trades': result['num_trades'],
        'win_rate': result['win_rate'],
        'stop_loss_count': result['stop_loss_count'],
    }


def main():
    print("=" * 60)
    print("快速因子验证 - 调仓日扫描")
    print("=" * 60)
    
    # 加载数据
    print("\n[1/3] 加载数据...")
    db = ETFDatabase()
    market_df = db.get_market_data()
    bench_df = db.get_market_data(ticker=BENCHMARK)
    sector_df = db.get_sector_data()
    
    market_df = market_df[market_df['ticker'] != BENCHMARK]
    
    print(f"  ETF: {market_df['ticker'].nunique()}只, {len(market_df)}条")
    print(f"  基准: {len(bench_df)}条")
    
    # 预计算评分（只执行一次）
    print("\n[2/3] 预计算评分和信号...")
    signals_df, scores_df = precompute_scores(market_df, bench_df, sector_df)
    print(f"  信号数据: {len(signals_df)}条")
    
    # 测试不同调仓日
    print("\n[3/3] 测试调仓日因子 (周一~周五)...")
    results = []
    for weekday in range(5):
        print(f"  测试周{['一','二','三','四','五'][weekday]}...", end=" ")
        result = test_rebalance_weekday(signals_df.copy(), market_df.copy(), bench_df.copy(), weekday, 'all')
        print(f"夏普={result['sharpe_ratio']:.3f} 收益={result['total_return']:.2%} 回撤={result['max_drawdown']:.2%}")
        results.append(result)
    
    # 整理结果
    results_df = pd.DataFrame(results)
    results_df = results_df.sort_values('sharpe_ratio', ascending=False)
    
    print(f"\n{'='*60}")
    print("调仓日因子测试结果 (按夏普排序)")
    print(f"{'='*60}")
    
    for idx, row in results_df.iterrows():
        rank = results_df.index.get_loc(idx) + 1
        print(f"\n  排名 #{rank}: {row['weekday_name']}")
        print(f"    收益: {row['total_return']:.2%} (年化{row['annual_return']:.2%})")
        print(f"    夏普: {row['sharpe_ratio']:.3f} / 回撤: {row['max_drawdown']:.2%}")
        print(f"    交易: {int(row['num_trades'])}次 / 胜率: {row['win_rate']:.1%} / 止损: {int(row['stop_loss_count'])}次")
    
    # 最佳调仓日
    best = results_df.iloc[0]
    print(f"\n{'='*60}")
    print(f"结论: 最佳调仓日 = {best['weekday_name']} (夏普={best['sharpe_ratio']:.3f})")
    print(f"{'='*60}")
    
    # 保存结果
    results_df.to_csv('reports/rebalance_weekday_test.csv', index=False, encoding='utf-8-sig')
    print(f"\n结果已保存: reports/rebalance_weekday_test.csv")


if __name__ == '__main__':
    main()
