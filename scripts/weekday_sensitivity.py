#!/usr/bin/env python3
"""
交易日敏感性实验：周一到周五调仓效果对比
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pandas as pd
from config import build_config, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK
from database import ETFDatabase
from backtest import BacktestEngine

WEEKDAY_MAP = {0: '周一', 1: '周二', 2: '周三', 3: '周四', 4: '周五'}

def main():
    db = ETFDatabase()
    b0_tickers = list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())
    market_df = db.get_market_data(ticker=b0_tickers)
    bench_df = db.get_market_data(ticker=BENCHMARK)
    
    print("=" * 90)
    print("交易日本敏感性实验：周一到周五调仓效果对比")
    print("=" * 90)
    
    results = []
    
    for wd in range(5):
        cfg = build_config()
        cfg['fallback_equity_enabled'] = False
        cfg['rebalance_weekday'] = wd  # 0=周一, 1=周二, ..., 3=周四, 4=周五
        
        engine = BacktestEngine(cfg)
        result = engine.run(market_df, bench_df)
        
        results.append({
            'weekday': wd,
            'weekday_name': WEEKDAY_MAP[wd],
            'total_return': result['total_return'],
            'annual_return': result['annual_return'],
            'sharpe_ratio': result['sharpe_ratio'],
            'max_drawdown': result['max_drawdown'],
            'num_trades': result['num_trades'],
        })
        
        print(f"\n[{WEEKDAY_MAP[wd]}] 调仓")
        print(f"  总收益: {result['total_return']:.2%}")
        print(f"  年化:   {result['annual_return']:.2%}")
        print(f"  夏普:   {result['sharpe_ratio']:.3f}")
        print(f"  回撤:   {result['max_drawdown']:.2%}")
        print(f"  交易:   {result['num_trades']} 笔")
    
    # 汇总对比
    print("\n" + "=" * 90)
    print("汇总对比")
    print("=" * 90)
    
    df = pd.DataFrame(results)
    df_sorted = df.sort_values('total_return', ascending=False)
    
    print(f"\n{'排名':<4} {'调仓日':<6} {'总收益':>10} {'年化':>10} {'夏普':>8} {'最大回撤':>10} {'交易笔数':>8}")
    print("-" * 65)
    for i, row in df_sorted.iterrows():
        marker = "  <<< 当前" if row['weekday'] == 3 else ""
        print(f"{i+1:<4} {row['weekday_name']:<6} {row['total_return']:>10.2%} "
              f"{row['annual_return']:>10.2%} {row['sharpe_ratio']:>8.3f} "
              f"{row['max_drawdown']:>10.2%} {row['num_trades']:>8}{marker}")
    
    # 计算敏感性
    returns = df['total_return'].values
    best = returns.max()
    worst = returns.min()
    spread = best - worst
    
    print(f"\n{'=' * 90}")
    print("敏感性分析")
    print(f"  最佳调仓日: {df_sorted.iloc[0]['weekday_name']} ({best:.2%})")
    print(f"  最差调仓日: {df_sorted.iloc[-1]['weekday_name']} ({worst:.2%})")
    print(f"  收益极差: {spread:.2%}")
    print(f"  标准差: {returns.std():.2%}")
    print(f"  变异系数: {returns.std() / returns.mean():.3f}")
    
    if spread < 0.05:
        print(f"  结论: 调仓日不敏感（极差<5%）")
    elif spread < 0.10:
        print(f"  结论: 调仓日中等敏感（极差5-10%）")
    else:
        print(f"  结论: 调仓日高敏感（极差>10%）")
    
    # 保存报告
    report = []
    report.append('# 交易日本敏感性实验报告')
    report.append('')
    report.append('## 结果汇总')
    report.append('')
    report.append('| 排名 | 调仓日 | 总收益 | 年化 | 夏普 | 最大回撤 | 交易笔数 |')
    report.append('|------|--------|--------|------|------|----------|----------|')
    for i, row in df_sorted.iterrows():
        marker = '当前' if row['weekday'] == 3 else ''
        report.append(f"| {i+1} | {row['weekday_name']} | {row['total_return']:.2%} | "
                     f"{row['annual_return']:.2%} | {row['sharpe_ratio']:.3f} | "
                     f"{row['max_drawdown']:.2%} | {row['num_trades']} | {marker}")
    report.append('')
    report.append(f"## 敏感性结论")
    report.append('')
    report.append(f"- 收益极差: {spread:.2%}")
    report.append(f"- 标准差: {returns.std():.2%}")
    report.append(f"- 变异系数: {returns.std() / returns.mean():.3f}")
    if spread < 0.05:
        report.append('- **结论: 调仓日不敏感（极差<5%），策略对调仓日选择不敏感**')
    elif spread < 0.10:
        report.append('- **结论: 调仓日中等敏感（极差5-10%）**')
    else:
        report.append('- **结论: 调仓日高敏感（极差>10%），需要谨慎选择调仓日**')
    report.append('')
    
    report_path = 'D:/etf_rotation_model/reports/weekday_sensitivity.md'
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(report))
    print(f"\n报告已保存: {report_path}")

if __name__ == '__main__':
    main()
