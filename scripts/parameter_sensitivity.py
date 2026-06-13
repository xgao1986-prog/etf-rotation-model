"""
参数敏感性实验 - 在默认参数基础上，逐个扫描关键参数
运行方式：python parameter_sensitivity.py
输出：参数扫描结果表
"""

import sys, os, json, time
sys.path.insert(0, 'src')

from database import ETFDatabase
from config import ALL_TRADABLE_ETFS, BENCHMARK, build_config
from backtest import BacktestEngine

def run_backtest_with_params(param_changes):
    """用指定参数覆盖运行回测，返回关键指标"""
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

# 基准参数（104.76%的参数）
baseline = {
    'market_timing': False,
    'fallback_equity_enabled': False,
    'defense_enabled': True,
    'total_max_holdings': 5,
    'max_holdings': 5,
    'max_position_per_etf': 0.15,
    'min_total_score': 40,
    'min_trend_score': 15,
    'min_confirm_score': 4,
    'stop_loss': -0.08,
    'defense_fill_max_ratio_bull': 0.30,
    'defense_fill_max_ratio_bear': 0.50,
    'cooling_period': 5,
    'trailing_stop_mode': 'tiered',
}

# 扫描定义：{参数名: [待测值列表]}
scan_params = {
    'min_total_score': [30, 35, 40, 45, 50],
    'max_holdings': [3, 4, 5, 6, 7],
    'max_position_per_etf': [0.10, 0.15, 0.20, 0.25, 0.30],
    'stop_loss': [-0.05, -0.06, -0.08, -0.10, -0.12],
    'defense_fill_max_ratio_bull': [0.0, 0.15, 0.30, 0.50, 0.70],
    'defense_fill_max_ratio_bear': [0.0, 0.30, 0.50, 0.70, 1.0],
    'cooling_period': [0, 3, 5, 10, 15],
    'trailing_stop_mode': ['none', 'simple', 'tiered'],
}

results = []
total_tests = sum(len(v) for v in scan_params.values())
count = 0

print(f"=== 参数敏感性实验 ===")
print(f"基准参数: {json.dumps(baseline, ensure_ascii=False)}")
print(f"总测试数: {total_tests}")
print()

# 先跑基准
print("Running baseline...")
baseline_result = run_backtest_with_params(baseline)
if baseline_result:
    results.append({
        'param': 'baseline',
        'value': '-',
        **baseline_result
    })
    print(f"  Baseline: 收益={baseline_result['total_return']:.2%}, 夏普={baseline_result['sharpe_ratio']:.2f}, 回撤={baseline_result['max_drawdown']:.2%}")

print()

# 逐个参数扫描
for param_name, values in scan_params.items():
    print(f"Scanning {param_name}: {values}")
    for value in values:
        count += 1
        test_params = baseline.copy()
        test_params[param_name] = value
        
        start = time.time()
        result = run_backtest_with_params(test_params)
        elapsed = time.time() - start
        
        if result:
            results.append({
                'param': param_name,
                'value': value,
                **result
            })
            print(f"  [{count}/{total_tests}] {param_name}={value}: 收益={result['total_return']:.2%}, 夏普={result['sharpe_ratio']:.2f}, 回撤={result['max_drawdown']:.2%}, 交易={result['num_trades']}, 时间={elapsed:.1f}s")
        else:
            print(f"  [{count}/{total_tests}] {param_name}={value}: FAILED")
    print()

# 保存结果
import pandas as pd

df = pd.DataFrame(results)
output_path = 'reports/parameter_sensitivity.csv'
df.to_csv(output_path, index=False, encoding='utf-8-sig')
print(f"结果已保存: {output_path}")

# 输出最佳参数
print("\n=== 各参数最佳值（按夏普）===")
for param_name in scan_params.keys():
    param_df = df[df['param'] == param_name]
    if not param_df.empty:
        best = param_df.loc[param_df['sharpe_ratio'].idxmax()]
        print(f"{param_name}: 最佳值={best['value']}, 夏普={best['sharpe_ratio']:.2f}, 收益={best['total_return']:.2%}, 回撤={best['max_drawdown']:.2%}")

print("\n=== 各参数最佳值（按收益）===")
for param_name in scan_params.keys():
    param_df = df[df['param'] == param_name]
    if not param_df.empty:
        best = param_df.loc[param_df['total_return'].idxmax()]
        print(f"{param_name}: 最佳值={best['value']}, 收益={best['total_return']:.2%}, 夏普={best['sharpe_ratio']:.2f}, 回撤={best['max_drawdown']:.2%}")

print("\n=== 各参数最佳值（按回撤最小）===")
for param_name in scan_params.keys():
    param_df = df[df['param'] == param_name]
    if not param_df.empty:
        best = param_df.loc[param_df['max_drawdown'].idxmax()]  # max_drawdown is negative, so idxmax = least negative
        print(f"{param_name}: 最佳值={best['value']}, 回撤={best['max_drawdown']:.2%}, 收益={best['total_return']:.2%}, 夏普={best['sharpe_ratio']:.2f}")
