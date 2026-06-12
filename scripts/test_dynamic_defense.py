"""
动态防御比例对比测试
对比：阶梯式 vs 线性插值
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pandas as pd
import config
from config import ETF_UNIVERSE, BENCHMARK, BACKTEST_CONFIG, build_config
from backtest import BacktestEngine
from database import ETFDatabase


def run_dynamic_test(mode, label, sample='all'):
    """运行特定防御比例模式的回测"""
    # 临时修改防御比例模式
    original_mode = config.DEFENSE_ALLOCATION_MODE
    config.DEFENSE_ALLOCATION_MODE = mode
    
    db = ETFDatabase()
    
    # 加载数据
    all_tickers = list(ETF_UNIVERSE.keys())
    market_df = db.get_market_data(ticker=all_tickers)
    bench_df = db.get_market_data(ticker=BENCHMARK)
    
    if market_df.empty or bench_df.empty:
        config.DEFENSE_ALLOCATION_MODE = original_mode
        return None
    
    # 过滤区间
    if sample == 'in':
        end = BACKTEST_CONFIG['in_sample_end']
        market_df = market_df[market_df['date'] <= end]
        bench_df = bench_df[bench_df['date'] <= end]
    elif sample == 'out':
        start = BACKTEST_CONFIG['out_sample_start']
        market_df = market_df[market_df['date'] >= start]
        bench_df = bench_df[bench_df['date'] >= start]
    
    # 构建配置
    trading_rules_cfg = {
        'rebalance_freq': 'weekly',
        'rebalance_weekday': 4,
        'cooling_period': 0,
        'cooling_score_boost': 0,
        'trailing_stop_mode': 'none',
    }
    
    cfg = build_config(trading_rules_cfg=trading_rules_cfg)
    engine = BacktestEngine(cfg)
    result = engine.run(market_df, bench_df)
    
    # 恢复原始配置
    config.DEFENSE_ALLOCATION_MODE = original_mode
    
    if 'error' in result:
        return None
    
    return {
        'label': label,
        'sample': sample,
        'mode': mode,
        'total_return': result['total_return'],
        'annual_return': result['annual_return'],
        'sharpe_ratio': result['sharpe_ratio'],
        'max_drawdown': result['max_drawdown'],
        'num_trades': result['num_trades'],
        'stop_loss_count': result['stop_loss_count'],
        'avg_holdings': result['avg_holdings'],
    }


def main():
    print("=" * 70)
    print("动态防御比例对比测试：阶梯式 vs 线性插值")
    print("=" * 70)
    
    # 两种模式
    modes = [
        ('step', "阶梯式防御"),
        ('linear', "线性插值防御"),
    ]
    
    # 测试区间
    samples = ['all', 'in', 'out']
    
    results = []
    
    for mode, label in modes:
        for sample in samples:
            sample_label = {'all': '全区间', 'in': '样本内', 'out': '样本外'}[sample]
            print(f"\n[{label}] [{sample_label}] 测试中...", end=" ")
            
            result = run_dynamic_test(mode, label, sample)
            if result:
                print(f"夏普={result['sharpe_ratio']:.3f} 收益={result['total_return']:.2%} 回撤={result['max_drawdown']:.2%}")
                results.append(result)
            else:
                print("失败")
    
    # 整理结果
    print(f"\n{'='*70}")
    print("测试结果汇总")
    print(f"{'='*70}")
    
    df = pd.DataFrame(results)
    
    # 按样本分组展示
    for sample in samples:
        sample_label = {'all': '全区间', 'in': '样本内', 'out': '样本外'}[sample]
        print(f"\n【{sample_label}】")
        print("-" * 70)
        
        sample_df = df[df['sample'] == sample].sort_values('sharpe_ratio', ascending=False)
        
        for _, row in sample_df.iterrows():
            print(f"\n  {row['label']}")
            print(f"    收益: {row['total_return']:.2%} (年化{row['annual_return']:.2%})")
            print(f"    夏普: {row['sharpe_ratio']:.3f} / 回撤: {row['max_drawdown']:.2%}")
            print(f"    交易: {row['num_trades']}次 / 止损: {row['stop_loss_count']}次")
            print(f"    平均持仓: {row['avg_holdings']:.1f}只")
    
    # 保存结果
    df.to_csv('reports/dynamic_defense_comparison.csv', index=False, encoding='utf-8-sig')
    print(f"\n{'='*70}")
    print(f"结果已保存: reports/dynamic_defense_comparison.csv")
    print(f"{'='*70}")


if __name__ == '__main__':
    main()
