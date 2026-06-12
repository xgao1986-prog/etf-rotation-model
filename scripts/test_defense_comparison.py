"""
防御模块对比测试脚本
测试三种防御配置：仅黄金 / 仅国债 / 黄金+国债
含样本内/外分离验证
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pandas as pd
from config import ETF_UNIVERSE, BENCHMARK, BACKTEST_CONFIG, build_config
from backtest import BacktestEngine
from database import ETFDatabase


def run_defense_test(defense_tickers, label, sample='all'):
    """运行特定防御配置的回测"""
    import config
    
    # 临时替换防御资产池
    original_defense = config.DEFENSE_UNIVERSE.copy()
    config.DEFENSE_UNIVERSE = {k: v for k, v in original_defense.items() if k in defense_tickers}
    
    db = ETFDatabase()
    
    # 加载数据
    all_tickers = list(ETF_UNIVERSE.keys())
    market_df = db.get_market_data(ticker=all_tickers)
    bench_df = db.get_market_data(ticker=BENCHMARK)
    
    if market_df.empty or bench_df.empty:
        config.DEFENSE_UNIVERSE = original_defense
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
    config.DEFENSE_UNIVERSE = original_defense
    
    if 'error' in result:
        return None
    
    # 统计防御资产交易
    trades = result['trades_df']
    defense_trades = trades[trades['ticker'].isin(defense_tickers)] if not trades.empty else pd.DataFrame()
    
    return {
        'label': label,
        'sample': sample,
        'defense_tickers': ','.join(defense_tickers),
        'total_return': result['total_return'],
        'annual_return': result['annual_return'],
        'sharpe_ratio': result['sharpe_ratio'],
        'max_drawdown': result['max_drawdown'],
        'num_trades': result['num_trades'],
        'stop_loss_count': result['stop_loss_count'],
        'defense_trades': len(defense_trades),
        'avg_holdings': result['avg_holdings'],
    }


def main():
    print("=" * 70)
    print("防御模块对比测试：黄金 vs 国债 vs 黄金+国债")
    print("=" * 70)
    
    # 三种防御配置
    configs = [
        (['518880.SH'], "仅黄金防御"),
        (['511010.SH'], "仅国债防御"),
        (['518880.SH', '511010.SH'], "黄金+国债双防御"),
    ]
    
    # 测试区间
    samples = ['all', 'in', 'out']
    
    results = []
    
    for defense_tickers, label in configs:
        for sample in samples:
            sample_label = {'all': '全区间', 'in': '样本内', 'out': '样本外'}[sample]
            print(f"\n[{label}] [{sample_label}] 测试中...", end=" ")
            
            result = run_defense_test(defense_tickers, label, sample)
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
            print(f"    交易: {row['num_trades']}次 / 止损: {row['stop_loss_count']}次 / 防御交易: {row['defense_trades']}次")
            print(f"    平均持仓: {row['avg_holdings']:.1f}只")
    
    # 保存结果
    df.to_csv('reports/defense_comparison.csv', index=False, encoding='utf-8-sig')
    print(f"\n{'='*70}")
    print(f"结果已保存: reports/defense_comparison.csv")
    print(f"{'='*70}")


if __name__ == '__main__':
    main()
