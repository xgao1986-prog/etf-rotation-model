"""
A/B测试脚本 - 测试不同改进场景对回测结果的影响
不修改核心代码，通过配置参数和实验性开关进行测试
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pandas as pd
import numpy as np
from copy import deepcopy
from datetime import datetime

from config import build_config, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK, BACKTEST_CONFIG
from database import ETFDatabase
from backtest import BacktestEngine
from strategy import StrategyEngine

# Monkey patch BacktestEngine._rebalance_v2 for experiment P0
original_rebalance_v2 = BacktestEngine._rebalance_v2

def _rebalance_v2_experiment(self, portfolio, day_signals, day_prices, effective_close_prices,
                              last_valid_close, date, date_str, buy_signals, trade_records,
                              cooling_list, max_total_position, _core_tickers, _fallback_tickers,
                              _defense_tickers, etf_group_map, same_group_max, rank_buffer_enabled,
                              buy_rank_n, sell_rank_n, candidate_rank, exit_debounce,
                              min_hold_for_candidate_exit, corr_matrix, corr_threshold,
                              calc_commission):
    """
    实验版本：支持P0（空仓强制防御）和P3（空仓加速入场）
    """
    # 实验参数
    use_empty_defense = self.cfg.get('experiment_empty_defense', False)
    empty_accel = self.cfg.get('experiment_empty_accel', False)
    
    # 如果启用了空仓强制防御，修改防御候选的生成逻辑
    if use_empty_defense:
        # 检查是否空仓
        current_positions = {t: p['shares'] for t, p in portfolio['positions'].items()}
        is_empty = len(current_positions) == 0
        
        # 如果空仓，放宽防御资产的入场条件
        if is_empty:
            # 为防御资产添加额外候选（不依赖MA20，只要有价格即可）
            for ticker in _defense_tickers:
                if ticker in effective_close_prices and effective_close_prices[ticker] > 0:
                    # 检查是否已在buy_signals中
                    if ticker not in buy_signals['ticker'].values:
                        # 创建防御资产BUY信号（固定评分）
                        defense_row = pd.DataFrame([{
                            'ticker': ticker,
                            'total_score': 50.0,  # 固定中等评分
                            'signal_type': 'BUY',
                            'trend_score': 20,
                            'confirm_score': 10,
                            'momentum_rank': 12.5,
                            'volume_score': 5,
                            'vol_score': 10,
                        }])
                        buy_signals = pd.concat([buy_signals, defense_row], ignore_index=True)
    
    # 调用原始逻辑
    return original_rebalance_v2(self, portfolio, day_signals, day_prices, effective_close_prices,
                                  last_valid_close, date, date_str, buy_signals, trade_records,
                                  cooling_list, max_total_position, _core_tickers, _fallback_tickers,
                                  _defense_tickers, etf_group_map, same_group_max, rank_buffer_enabled,
                                  buy_rank_n, sell_rank_n, candidate_rank, exit_debounce,
                                  min_hold_for_candidate_exit, corr_matrix, corr_threshold,
                                  calc_commission)

# Monkey patch StrategyEngine.generate_signals for experiment P2 and P3
original_generate_signals = StrategyEngine.generate_signals

def generate_signals_experiment(self, scores_df, bench_df):
    """
    实验版本：支持P2（MA20缓冲）和P3（空仓加速入场）
    """
    # 先调用原始逻辑
    result = original_generate_signals(self, scores_df, bench_df)
    
    # 实验参数
    ma20_buffer = self.cfg.get('experiment_ma20_sell_buffer', 1.0)  # 1.0 = 无缓冲
    use_empty_accel = self.cfg.get('experiment_empty_accel', False)
    
    # P2: MA20卖出缓冲
    if ma20_buffer < 1.0:
        # 重新计算sell_mask，使用缓冲
        result.loc[result['prev_close'] < result['ma20'] * ma20_buffer, 'signal_type'] = 'SELL'
    
    # P3: 空仓加速入场（需要跟踪空仓状态，在backtest层实现）
    # 这里只准备降低门槛的条件，实际在backtest中使用
    if use_empty_accel:
        # 标记为"加速候选"（需要backtest层配合）
        result['accel_candidate'] = False
        accel_mask = (
            result['ticker'].isin(list(ETF_UNIVERSE.keys())) &
            (result['total_score'] >= 35) &  # 降低10分
            (result['prev_close'] > result['ma20'] * 0.99) &  # 1%缓冲
            (result['ma20_slope'] > -0.001)  # 允许微跌
        )
        result.loc[accel_mask, 'accel_candidate'] = True
    
    return result

def run_scenario(name, cfg_override, market_df, bench_df):
    """运行单个场景回测"""
    cfg = build_config()
    cfg['fallback_equity_enabled'] = False
    cfg.update(cfg_override)
    
    engine = BacktestEngine(cfg)
    result = engine.run(market_df, bench_df)
    
    return {
        'name': name,
        'total_return': result['total_return'],
        'annual_return': result['annual_return'],
        'sharpe_ratio': result['sharpe_ratio'],
        'max_drawdown': result['max_drawdown'],
        'num_trades': result['num_trades'],
        'avg_holdings': result['avg_holdings'],
    }

def main():
    print("=" * 60)
    print("A/B测试 - 改进场景对比")
    print("=" * 60)
    
    # 加载数据
    db = ETFDatabase()
    b0_tickers = list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())
    market_df = db.get_market_data(ticker=b0_tickers)
    bench_df = db.get_market_data(ticker=BENCHMARK)
    
    print(f"\n数据: {len(market_df)} 行, {market_df['ticker'].nunique()} 只")
    print(f"日期: {market_df['date'].min()} ~ {market_df['date'].max()}")
    
    # 定义测试场景
    scenarios = [
        ('A. 原始逻辑', {}),
        ('B. P0: 空仓强制防御', {'experiment_empty_defense': True}),
        ('C. P2: MA20卖出缓冲2%', {'experiment_ma20_sell_buffer': 0.98}),
        ('D. P3: 空仓加速入场', {'experiment_empty_accel': True}),
        ('E. P0+P2 组合', {'experiment_empty_defense': True, 'experiment_ma20_sell_buffer': 0.98}),
        ('F. P0+P2+P3 组合', {'experiment_empty_defense': True, 'experiment_ma20_sell_buffer': 0.98, 'experiment_empty_accel': True}),
    ]
    
    # 应用monkey patch
    BacktestEngine._rebalance_v2 = _rebalance_v2_experiment
    StrategyEngine.generate_signals = generate_signals_experiment
    
    results = []
    for name, cfg_override in scenarios:
        print(f"\n[Running] {name}...")
        try:
            result = run_scenario(name, cfg_override, market_df, bench_df)
            results.append(result)
            print(f"  总收益: {result['total_return']:.2%}  年化: {result['annual_return']:.2%}  "
                  f"夏普: {result['sharpe_ratio']:.2f}  回撤: {result['max_drawdown']:.2%}  "
                  f"交易: {result['num_trades']}")
        except Exception as e:
            print(f"  [ERROR] {e}")
    
    # 对比报告
    print("\n" + "=" * 60)
    print("A/B测试对比结果")
    print("=" * 60)
    
    baseline = results[0] if results else None
    
    print(f"\n{'场景':<20} {'总收益':>10} {'年化':>10} {'夏普':>8} {'回撤':>10} {'交易':>8} {'总收益Δ':>10}")
    print("-" * 80)
    for r in results:
        delta = r['total_return'] - baseline['total_return'] if baseline else 0
        print(f"{r['name']:<20} {r['total_return']:>10.2%} {r['annual_return']:>10.2%} "
              f"{r['sharpe_ratio']:>8.2f} {r['max_drawdown']:>10.2%} {r['num_trades']:>8} {delta:>+10.2%}")
    
    # 找出最佳场景
    if results:
        best = max(results, key=lambda x: x['total_return'])
        print(f"\n最佳场景: {best['name']} (总收益 {best['total_return']:.2%})")

if __name__ == '__main__':
    main()
