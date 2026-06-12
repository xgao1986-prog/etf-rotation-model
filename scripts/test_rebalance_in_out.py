#!/usr/bin/env python3
"""
调仓日样本内/外验证脚本
对比周四 vs 周五在样本内(2019-2023)和样本外(2024-2026)的表现
"""

import sys
sys.path.insert(0, 'src')

import pandas as pd
from database import ETFDatabase
from backtest import BacktestEngine
from config import ETF_UNIVERSE, BENCHMARK, STRATEGY_CONFIG


def run_backtest_with_weekday(weekday, weekday_name, sample_type):
    """运行指定调仓日和样本区间的回测"""
    
    db = ETFDatabase()
    
    # 加载数据
    etf_tickers = list(ETF_UNIVERSE.keys())
    market_df = db.get_market_data(ticker=etf_tickers)
    bench_df = db.get_market_data(ticker=BENCHMARK)
    
    if market_df.empty or bench_df.empty:
        return None
    
    # 修改配置
    cfg = STRATEGY_CONFIG.copy()
    cfg['rebalance_weekday'] = weekday
    
    # 运行回测
    engine = BacktestEngine(cfg)
    
    if sample_type == 'in':
        result = engine.run_in_sample(market_df, bench_df)
        label = '样本内(2019-2023)'
    elif sample_type == 'out':
        result = engine.run_out_sample(market_df, bench_df)
        label = '样本外(2024-2026)'
    else:
        result = engine.run(market_df, bench_df)
        label = '全区间'
    
    if 'error' in result:
        return None
    
    trades_df = result.get('trades_df', pd.DataFrame())
    total_trades = len(trades_df) if not trades_df.empty else 0
    
    return {
        '调仓日': weekday_name,
        '区间': label,
        '总收益率': result['total_return'],
        '年化收益率': result['annual_return'],
        '夏普比率': result['sharpe_ratio'],
        '最大回撤': result['max_drawdown'],
        '年化波动率': result['volatility'],
        '交易次数': total_trades,
        '胜率': result['win_rate'],
        '平均持仓': result['avg_holdings'],
    }


def main():
    """主函数：测试周四vs周五在样本内/外的表现"""
    
    print("=" * 80)
    print("调仓日样本内/外验证：周四 vs 周五")
    print("=" * 80)
    print()
    
    results = []
    
    # 测试组合：(周四/周五) x (样本内/样本外)
    test_cases = [
        (4, '周五', 'in'),
        (3, '周四', 'in'),
        (4, '周五', 'out'),
        (3, '周四', 'out'),
    ]
    
    for weekday, name, sample in test_cases:
        print(f"测试 {name} + {sample}...")
        result = run_backtest_with_weekday(weekday, name, sample)
        if result:
            results.append(result)
            print(f"  收益: {result['总收益率']:.2%}, 夏普: {result['夏普比率']:.2f}, 回撤: {result['最大回撤']:.2%}, 交易: {result['交易次数']}")
    
    # 汇总
    if results:
        df = pd.DataFrame(results)
        
        print()
        print("=" * 80)
        print("验证结果汇总")
        print("=" * 80)
        print()
        
        # 按区间分组显示
        for sample_label in ['样本内(2019-2023)', '样本外(2024-2026)']:
            sample_df = df[df['区间'] == sample_label]
            if not sample_df.empty:
                print(f"\n{sample_label}:")
                print(sample_df[['调仓日', '总收益率', '年化收益率', '夏普比率', '最大回撤', '交易次数']].to_string(
                    index=False, 
                    float_format=lambda x: f'{x:.2%}' if abs(x) < 1 else f'{x:.2f}'
                ))
                
                # 计算差异
                if len(sample_df) == 2:
                    thu = sample_df[sample_df['调仓日'] == '周四'].iloc[0]
                    fri = sample_df[sample_df['调仓日'] == '周五'].iloc[0]
                    print(f"\n  周四 vs 周五差异:")
                    print(f"    总收益: {thu['总收益率'] - fri['总收益率']:+.2%}")
                    print(f"    夏普: {thu['夏普比率'] - fri['夏普比率']:+.2f}")
                    print(f"    回撤: {thu['最大回撤'] - fri['最大回撤']:+.2%}")
        
        # 保存
        output_path = 'reports/rebalance_day_in_out_sample.csv'
        df.to_csv(output_path, index=False, encoding='utf-8-sig')
        print(f"\n结果已保存: {output_path}")
    
    return results


if __name__ == '__main__':
    main()
