"""
动态止盈因子快速验证
测试 simple 和 tiered 两种模式
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from config import build_config, FACTOR_CONFIG
from backtest import BacktestEngine
from database import ETFDatabase
from strategy import StrategyEngine


def test_trailing_stop_mode(mode, **kwargs):
    """测试特定动态止盈模式"""
    factor_cfg = FACTOR_CONFIG.copy()
    factor_cfg['trailing_stop_mode'] = mode
    for k, v in kwargs.items():
        factor_cfg[k] = v
    
    cfg = build_config(factor_cfg=factor_cfg)
    
    # 加载数据
    db = ETFDatabase()
    market_df = db.get_market_data()
    bench_df = db.get_market_data(ticker='000300.SH')
    sector_df = db.get_sector_data()
    market_df = market_df[market_df['ticker'] != '000300.SH']
    
    # 预计算
    engine = BacktestEngine(cfg)
    strategy = StrategyEngine(cfg)
    
    all_scores = []
    for ticker in market_df['ticker'].unique():
        ticker_df = market_df[market_df['ticker'] == ticker].copy()
        if len(ticker_df) < 50:
            continue
        scored = strategy.calculate_total_score(ticker_df, None)
        all_scores.append(scored)
    
    scores_df = pd.concat(all_scores, ignore_index=True)
    signals_df = strategy.generate_signals(scores_df, bench_df)
    
    result = engine._execute_backtest(signals_df, market_df, bench_df)
    
    return {
        'mode': mode,
        'params': kwargs,
        'total_return': result['total_return'],
        'sharpe_ratio': result['sharpe_ratio'],
        'max_drawdown': result['max_drawdown'],
        'num_trades': result['num_trades'],
        'stop_loss_count': result['stop_loss_count'],
    }


if __name__ == '__main__':
    import pandas as pd
    
    print("=" * 60)
    print("动态止盈因子验证")
    print("=" * 60)
    
    # 测试1: none 模式（不启用动态止盈）
    print("\n[1/4] 测试 none 模式（不启用动态止盈）...")
    r1 = test_trailing_stop_mode('none')
    print(f"  夏普={r1['sharpe_ratio']:.3f} 收益={r1['total_return']:.2%} 回撤={r1['max_drawdown']:.2%} 止损={r1['stop_loss_count']}次")
    
    # 测试2: simple 模式，回撤5%止盈
    print("\n[2/4] 测试 simple 模式（回撤5%止盈）...")
    r2 = test_trailing_stop_mode('simple', trailing_stop=-0.05)
    print(f"  夏普={r2['sharpe_ratio']:.3f} 收益={r2['total_return']:.2%} 回撤={r2['max_drawdown']:.2%} 止损={r2['stop_loss_count']}次")
    
    # 测试3: simple 模式，回撤10%止盈
    print("\n[3/4] 测试 simple 模式（回撤10%止盈）...")
    r3 = test_trailing_stop_mode('simple', trailing_stop=-0.10)
    print(f"  夏普={r3['sharpe_ratio']:.3f} 收益={r3['total_return']:.2%} 回撤={r3['max_drawdown']:.2%} 止损={r3['stop_loss_count']}次")
    
    # 测试4: tiered 模式（默认分档）
    print("\n[4/4] 测试 tiered 模式（默认分档）...")
    r4 = test_trailing_stop_mode('tiered')
    print(f"  夏普={r4['sharpe_ratio']:.3f} 收益={r4['total_return']:.2%} 回撤={r4['max_drawdown']:.2%} 止损={r4['stop_loss_count']}次")
    
    # 汇总
    print(f"\n{'='*60}")
    print("动态止盈模式对比")
    print(f"{'='*60}")
    
    results = [r1, r2, r3, r4]
    for r in results:
        print(f"\n  {r['mode']:8s}: 夏普={r['sharpe_ratio']:.3f} 收益={r['total_return']:.2%} 回撤={r['max_drawdown']:.2%}")
    
    print(f"\n{'='*60}")
    print("验证完成")
    print(f"{'='*60}")
