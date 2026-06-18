# -*- coding: utf-8 -*-
"""
v5_test.py - 测试修复后回测引擎并检查缺失价格日志
"""
import sys
sys.path.insert(0, 'src')

import pandas as pd
from database import ETFDatabase
from backtest import BacktestEngine
import config


def load_data():
    db = ETFDatabase()
    tickers = list(config.ETF_UNIVERSE.keys()) + list(config.DEFENSE_UNIVERSE.keys())
    market_dfs = []
    for ticker in tickers:
        df = db.get_market_data(ticker=ticker)
        if not df.empty:
            market_dfs.append(df)
    market_df = pd.concat(market_dfs, ignore_index=True) if market_dfs else pd.DataFrame()
    market_df['date'] = pd.to_datetime(market_df['date'])
    bench_df = db.get_market_data(ticker=config.BENCHMARK)
    bench_df['date'] = pd.to_datetime(bench_df['date'])
    return market_df, bench_df


print("[1/3] 加载数据...")
market_df, bench_df = load_data()
print(f"  市场数据: {len(market_df)} 行, {market_df['ticker'].nunique()} 只ETF")
print(f"  基准数据: {len(bench_df)} 行")

print("\n[2/3] 运行修复后回测...")
engine = BacktestEngine()
result = engine.run(market_df, bench_df)
if 'error' in result:
    print(f"  回测失败: {result['error']}")
    sys.exit(1)

print(f"  总收益: {result['total_return']:.2%}")
print(f"  最大回撤: {result['max_drawdown']:.2%}")
print(f"  夏普: {result['sharpe_ratio']:.2f}")
print(f"  交易次数: {result['num_trades']}")
print(f"  总佣金: {result['total_commission']:,.2f}")

print("\n[3/3] 检查缺失价格日志...")
missing_log = result.get('missing_price_log', pd.DataFrame())
if missing_log.empty:
    print("  无缺失价格记录（所有ETF在所有交易日都有数据）")
else:
    print(f"  缺失价格记录数: {len(missing_log)}")
    
    # 按ETF汇总
    by_ticker = missing_log.groupby('ticker').agg(
        total_events=('date', 'count'),
        total_consecutive_days=('consecutive_missing_days', 'max'),
        total_impact=('impact', 'sum'),
    ).sort_values('total_impact', ascending=False)
    
    print(f"\n  按ETF汇总:")
    print(by_ticker.to_string())
    
    # 按日期查看最大冲击
    by_date = missing_log.groupby('date').agg(
        num_etfs=('ticker', 'count'),
        total_impact=('impact', 'sum'),
    ).sort_values('total_impact', ascending=False)
    
    print(f"\n  按日期冲击最大的10天:")
    print(by_date.head(10).to_string())
    
    # 输出缺失价格日志文件
    missing_log.to_csv('reports/missing_price_log_v5.csv', index=False, encoding='utf-8-sig')
    print(f"\n  缺失价格日志已保存: reports/missing_price_log_v5.csv")

print(f"\n[OK] 修复后回测完成")
