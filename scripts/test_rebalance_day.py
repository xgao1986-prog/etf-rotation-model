#!/usr/bin/env python3
"""
调仓日对比测试脚本
测试周一到周五不同调仓日的策略表现
"""

import sys
sys.path.insert(0, 'src')

import pandas as pd
from database import ETFDatabase
from backtest import BacktestEngine
from config import ETF_UNIVERSE, BENCHMARK, STRATEGY_CONFIG


def test_rebalance_day(weekday, weekday_name):
    """测试指定调仓日的回测表现"""
    
    db = ETFDatabase()
    
    # 加载数据
    etf_tickers = list(ETF_UNIVERSE.keys())
    market_df = db.get_market_data(ticker=etf_tickers)
    bench_df = db.get_market_data(ticker=BENCHMARK)
    
    if market_df.empty or bench_df.empty:
        print(f"[{weekday_name}] 数据库无数据")
        return None
    
    # 修改配置：调仓日
    cfg = STRATEGY_CONFIG.copy()
    cfg['rebalance_weekday'] = weekday
    
    # 运行回测
    engine = BacktestEngine(cfg)
    result = engine.run(market_df, bench_df)
    
    if 'error' in result:
        print(f"[{weekday_name}] 回测失败: {result['error']}")
        return None
    
    # 统计交易频率
    trades_df = result.get('trades_df', pd.DataFrame())
    if not trades_df.empty:
        buy_count = len(trades_df[trades_df['action'] == 'BUY'])
        sell_count = len(trades_df[trades_df['action'] == 'SELL'])
        stop_count = len(trades_df[trades_df['action'] == 'STOP_LOSS'])
        total_trades = len(trades_df)
    else:
        buy_count = sell_count = stop_count = total_trades = 0
    
    return {
        '调仓日': weekday_name,
        'weekday': weekday,
        '总收益率': result['total_return'],
        '年化收益率': result['annual_return'],
        '夏普比率': result['sharpe_ratio'],
        '最大回撤': result['max_drawdown'],
        '年化波动率': result['volatility'],
        '索提诺比率': result['sortino_ratio'],
        '交易次数': total_trades,
        '买入次数': buy_count,
        '调仓卖出': sell_count,
        '止损次数': stop_count,
        '胜率': result['win_rate'],
        '平均盈利': result['avg_win'],
        '平均亏损': result['avg_loss'],
        '平均持仓': result['avg_holdings'],
        '最大持仓': result['max_holdings'],
    }


def main():
    """主函数：测试周一到周五"""
    
    weekdays = [
        (0, '周一'),
        (1, '周二'),
        (2, '周三'),
        (3, '周四'),
        (4, '周五'),
    ]
    
    results = []
    
    print("=" * 80)
    print("调仓日对比测试（周一到周五）")
    print("=" * 80)
    print()
    
    for weekday, name in weekdays:
        print(f"\n测试 {name} 调仓...")
        result = test_rebalance_day(weekday, name)
        if result:
            results.append(result)
            print(f"  总收益: {result['总收益率']:.2%}, 夏普: {result['夏普比率']:.2f}, 回撤: {result['最大回撤']:.2%}, 交易: {result['交易次数']}")
    
    # 汇总对比
    if results:
        df = pd.DataFrame(results)
        df = df.sort_values('夏普比率', ascending=False)
        
        print()
        print("=" * 80)
        print("对比结果（按夏普排序）")
        print("=" * 80)
        print()
        
        # 格式化输出
        display_cols = ['调仓日', '总收益率', '年化收益率', '夏普比率', '最大回撤', '交易次数', '胜率']
        print(df[display_cols].to_string(index=False, float_format=lambda x: f'{x:.2%}' if abs(x) < 1 else f'{x:.2f}'))
        
        # 保存结果
        output_path = 'reports/rebalance_day_comparison.csv'
        df.to_csv(output_path, index=False, encoding='utf-8-sig')
        print()
        print(f"结果已保存: {output_path}")
        
        # 最佳调仓日
        best = df.iloc[0]
        print()
        print(f"最佳调仓日: {best['调仓日']} (夏普{best['夏普比率']:.2f})")
    
    return results


if __name__ == '__main__':
    main()
