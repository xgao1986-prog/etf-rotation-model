"""
测试：max_position_per_etf=0.20（满仓能力）+ 不同防御填充上限
同时测试：等权无上限 vs 固定上限
"""

import sys, os, json, time
sys.path.insert(0, 'src')

from database import ETFDatabase
from config import ALL_TRADABLE_ETFS, BENCHMARK, build_config
from backtest import BacktestEngine

def run_backtest(param_changes):
    cfg = build_config(strategy_cfg=param_changes)
    db = ETFDatabase()
    etf_tickers = list(ALL_TRADABLE_ETFS.keys())
    market_df = db.get_market_data(ticker=etf_tickers)
    bench_df = db.get_market_data(ticker=BENCHMARK)
    if market_df.empty or bench_df.empty:
        return None
    engine = BacktestEngine(cfg)
    result = engine.run(market_df, bench_df)
    if 'error' in result:
        return None
    return {
        'total_return': result['total_return'],
        'annual_return': result['annual_return'],
        'sharpe_ratio': result['sharpe_ratio'],
        'max_drawdown': result['max_drawdown'],
        'num_trades': result['num_trades'],
        'win_rate': result['win_rate'],
        'avg_holdings': result['avg_holdings'],
    }

print("=== 实验1：max_position_per_etf=0.20，不同防御填充上限 ===")
print()

for fill_max in [0.0, 0.15, 0.30, 0.50, 0.70, 1.0]:
    params = {
        'max_position_per_etf': 0.20,
        'defense_fill_max_ratio_bull': fill_max,
    }
    start = time.time()
    result = run_backtest(params)
    elapsed = time.time() - start
    if result:
        print(f"fill_max={fill_max:.0%}: 收益={result['total_return']:.2%}, 夏普={result['sharpe_ratio']:.2f}, 回撤={result['max_drawdown']:.2%}, 交易={result['num_trades']}, 持仓={result['avg_holdings']:.1f}, 时间={elapsed:.1f}s")
    else:
        print(f"fill_max={fill_max:.0%}: FAILED")

print()
print("=== 实验2：不同 max_position_per_etf 对比（默认 fill_max=30%） ===")
print()

for pos_limit in [0.10, 0.15, 0.20, 0.25, 0.33]:
    params = {
        'max_position_per_etf': pos_limit,
    }
    start = time.time()
    result = run_backtest(params)
    elapsed = time.time() - start
    if result:
        print(f"pos_limit={pos_limit:.0%}: 收益={result['total_return']:.2%}, 夏普={result['sharpe_ratio']:.2f}, 回撤={result['max_drawdown']:.2%}, 交易={result['num_trades']}, 持仓={result['avg_holdings']:.1f}, 时间={elapsed:.1f}s")
    else:
        print(f"pos_limit={pos_limit:.0%}: FAILED")

print()
print("=== 实验3：等权分配（不设上限）vs 固定上限 ===")
print()

# 不设上限 = 买入时把可用资金均分给所有入选ETF
# 这需要修改backtest逻辑，这里只能模拟：如果 max_position_per_etf=1.0，
# 买入逻辑会用 base_weight = min(1.0, 1.0/n_buy)，实际等于 100%/n_buy
for pos_limit in [0.20, 0.33, 0.50, 1.0]:
    params = {
        'max_position_per_etf': pos_limit,
        'defense_fill_max_ratio_bull': 0.0,  # 关闭防御填充，纯看等权效果
    }
    start = time.time()
    result = run_backtest(params)
    elapsed = time.time() - start
    if result:
        print(f"pos_limit={pos_limit:.0%} (no defense fill): 收益={result['total_return']:.2%}, 夏普={result['sharpe_ratio']:.2f}, 回撤={result['max_drawdown']:.2%}, 交易={result['num_trades']}, 持仓={result['avg_holdings']:.1f}, 时间={elapsed:.1f}s")
    else:
        print(f"pos_limit={pos_limit:.0%}: FAILED")

print()
print("=== 实验4：0.20上限 + 0/0.30/0.50 fill + 不同止损 ===")
print()

for stop in [-0.05, -0.08, -0.12]:
    for fill in [0.0, 0.30, 0.50]:
        params = {
            'max_position_per_etf': 0.20,
            'defense_fill_max_ratio_bull': fill,
            'stop_loss': stop,
        }
        start = time.time()
        result = run_backtest(params)
        elapsed = time.time() - start
        if result:
            print(f"stop={stop:.0%}, fill={fill:.0%}: 收益={result['total_return']:.2%}, 夏普={result['sharpe_ratio']:.2f}, 回撤={result['max_drawdown']:.2%}, 交易={result['num_trades']}")
        else:
            print(f"stop={stop:.0%}, fill={fill:.0%}: FAILED")
    print()
