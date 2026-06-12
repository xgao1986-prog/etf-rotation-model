#!/usr/bin/env python3
"""
按年度验证调仓日：周四 vs 周五
分析不同市场环境下的最优调仓日
"""

import sys
sys.path.insert(0, 'src')

import pandas as pd
from database import ETFDatabase
from backtest import BacktestEngine
from config import ETF_UNIVERSE, BENCHMARK, STRATEGY_CONFIG


def run_yearly_backtest(weekday, weekday_name, year):
    """运行指定年份和调仓日的回测"""
    
    db = ETFDatabase()
    
    # 加载数据
    etf_tickers = list(ETF_UNIVERSE.keys())
    start_date = f'{year}-01-01'
    end_date = f'{year}-12-31'
    
    market_df = db.get_market_data(ticker=etf_tickers, start_date=start_date, end_date=end_date)
    bench_df = db.get_market_data(ticker=BENCHMARK, start_date=start_date, end_date=end_date)
    
    if market_df.empty or bench_df.empty:
        return None
    
    # 修改配置
    cfg = STRATEGY_CONFIG.copy()
    cfg['rebalance_weekday'] = weekday
    
    # 运行回测
    engine = BacktestEngine(cfg)
    result = engine.run(market_df, bench_df)
    
    if 'error' in result:
        return None
    
    trades_df = result.get('trades_df', pd.DataFrame())
    total_trades = len(trades_df) if not trades_df.empty else 0
    
    return {
        '年份': year,
        '调仓日': weekday_name,
        '总收益率': result['total_return'],
        '年化收益率': result['annual_return'],
        '夏普比率': result['sharpe_ratio'],
        '最大回撤': result['max_drawdown'],
        '年化波动率': result['volatility'],
        '交易次数': total_trades,
        '胜率': result['win_rate'],
    }


def main():
    """主函数：按年测试周四vs周五"""
    
    print("=" * 80)
    print("按年度验证调仓日：周四 vs 周五")
    print("=" * 80)
    print()
    
    results = []
    years = [2019, 2020, 2021, 2022, 2023, 2024, 2025]
    
    for year in years:
        print(f"\n{'='*40} {year}年 {'='*40}")
        
        for weekday, name in [(4, '周五'), (3, '周四')]:
            print(f"  测试 {name}...", end='')
            result = run_yearly_backtest(weekday, name, year)
            if result:
                results.append(result)
                print(f" 收益{result['总收益率']:.2%} 夏普{result['夏普比率']:.2f} 回撤{result['最大回撤']:.2%}")
            else:
                print(" 无数据")
    
    # 汇总
    if results:
        df = pd.DataFrame(results)
        
        print()
        print("=" * 80)
        print("年度对比汇总")
        print("=" * 80)
        print()
        
        # 按年份显示
        for year in years:
            year_df = df[df['年份'] == year]
            if len(year_df) == 2:
                print(f"\n{year}年:")
                print(year_df[['调仓日', '总收益率', '夏普比率', '最大回撤', '交易次数']].to_string(
                    index=False,
                    float_format=lambda x: f'{x:.2%}' if abs(x) < 1 else f'{x:.2f}'
                ))
                
                thu = year_df[year_df['调仓日'] == '周四'].iloc[0]
                fri = year_df[year_df['调仓日'] == '周五'].iloc[0]
                winner = '周四' if thu['夏普比率'] > fri['夏普比率'] else '周五'
                print(f"  → 最优: {winner} (夏普 {max(thu['夏普比率'], fri['夏普比率']):.2f} vs {min(thu['夏普比率'], fri['夏普比率']):.2f})")
        
        # 统计周四胜率
        thu_wins = 0
        for year in years:
            year_df = df[df['年份'] == year]
            if len(year_df) == 2:
                thu = year_df[year_df['调仓日'] == '周四'].iloc[0]
                fri = year_df[year_df['调仓日'] == '周五'].iloc[0]
                if thu['夏普比率'] > fri['夏普比率']:
                    thu_wins += 1
        
        total_years = len([y for y in years if len(df[df['年份'] == y]) == 2])
        print(f"\n{'='*80}")
        print(f"统计：周四在 {thu_wins}/{total_years} 个年份中夏普更优")
        print(f"{'='*80}")
        
        # 保存
        output_path = 'reports/rebalance_day_by_year.csv'
        df.to_csv(output_path, index=False, encoding='utf-8-sig')
        print(f"\n结果已保存: {output_path}")
    
    return results


if __name__ == '__main__':
    main()
