#!/usr/bin/env python3
"""
B0.2 基准回测脚本（Phase 5.5）

在当前默认配置（momentum_factor_enabled=False）下运行完整回测，
生成 B0.2 基准报告，与 B0.1（历史冻结）对比。

输出：reports/baseline_B0.2_YYYYMMDD_HHMMSS.md
"""

import sys, os
import pandas as pd
import numpy as np
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from config import DB_PATH, BENCHMARK, STRATEGY_CONFIG, build_config
from database import ETFDatabase
from backtest import BacktestEngine

AS_OF_DATE = '2026-06-18'

def run_baseline():
    db = ETFDatabase()
    
    print("=" * 70)
    print(f"B0.2 基准回测 (截止至 {AS_OF_DATE})")
    print("  配置: momentum_factor_enabled=False (no_momentum)")
    print("=" * 70)
    
    from config import ETF_UNIVERSE, DEFENSE_UNIVERSE
    etf_tickers = list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())
    market_df = db.get_market_data(ticker=etf_tickers, start_date='2019-01-01', end_date=AS_OF_DATE)
    bench_df = db.get_market_data(ticker=BENCHMARK, start_date='2019-01-01', end_date=AS_OF_DATE)
    
    print(f"\n  行情数据: {len(market_df):,} 条")
    print(f"  标的数量: {market_df['ticker'].nunique()} 只")
    print(f"  日期范围: {market_df['date'].min()} ~ {market_df['date'].max()}")
    
    cfg = build_config()
    cfg['fallback_equity_enabled'] = False
    cfg['momentum_factor_enabled'] = False  # B0.2 关闭 momentum
    
    print(f"\n  配置确认:")
    print(f"    momentum_factor_enabled: {cfg['momentum_factor_enabled']}")
    print(f"    use_v2_rebalance: {cfg['use_v2_rebalance']}")
    print(f"    rebalance_weekday: {cfg['rebalance_weekday']}")
    print(f"    stop_loss: {cfg['stop_loss']:.0%}")
    print(f"    max_position_per_etf: {cfg['max_position_per_etf']:.0%}")
    
    engine = BacktestEngine(cfg)
    result = engine.run(market_df, bench_df, as_of_date=AS_OF_DATE)
    
    nav_df = result['nav_df']
    trades_df = result['trades_df']
    
    print(f"\n  回测结果:")
    print(f"    最终NAV: {nav_df['nav'].iloc[-1]:,.0f}")
    print(f"    总收益: {result['total_return']:.2%}")
    print(f"    年化收益: {result['annual_return']:.2%}")
    print(f"    夏普: {result['sharpe_ratio']:.4f}")
    print(f"    最大回撤: {result['max_drawdown']:.2%}")
    print(f"    交易次数: {result['num_trades']}")
    print(f"    买入次数: {result['buy_count']}")
    print(f"    卖出次数: {result['sell_count']}")
    print(f"    调仓次数: {result['rebalance_count']}")
    
    # 生成报告
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    report_path = os.path.join('reports', f'baseline_B0.2_{ts}.md')
    
    lines = []
    lines.append("# B0.2 基准回测报告")
    lines.append("")
    lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**数据截止**: {AS_OF_DATE}")
    lines.append("")
    lines.append("## 配置")
    lines.append("")
    lines.append(f"| 参数 | 值 |")
    lines.append(f"|------|-----|")
    lines.append(f"| momentum_factor_enabled | False |")
    lines.append(f"| use_v2_rebalance | {cfg['use_v2_rebalance']} |")
    lines.append(f"| rebalance_weekday | {cfg['rebalance_weekday']} |")
    lines.append(f"| stop_loss | {cfg['stop_loss']:.0%} |")
    lines.append(f"| max_position_per_etf | {cfg['max_position_per_etf']:.0%} |")
    lines.append(f"| min_total_score | {cfg['min_total_score']} |")
    lines.append("")
    lines.append("## 核心指标")
    lines.append("")
    lines.append(f"| 指标 | B0.2 |")
    lines.append(f"|------|------|")
    lines.append(f"| 最终NAV | {nav_df['nav'].iloc[-1]:,.0f} |")
    lines.append(f"| 总收益 | {result['total_return']:.2%} |")
    lines.append(f"| 年化收益 | {result['annual_return']:.2%} |")
    lines.append(f"| 夏普比率 | {result['sharpe_ratio']:.4f} |")
    lines.append(f"| 最大回撤 | {result['max_drawdown']:.2%} |")
    lines.append(f"| 交易次数 | {result['num_trades']} |")
    lines.append(f"| 买入次数 | {result['buy_count']} |")
    lines.append(f"| 卖出次数 | {result['sell_count']} |")
    lines.append(f"| 调仓次数 | {result['rebalance_count']} |")
    lines.append("")
    lines.append("## 与 B0.1 对比")
    lines.append("")
    lines.append("| 指标 | B0.1 (frozen) | B0.2 | Delta |")
    lines.append("|------|---------------|------|-------|")
    lines.append(f"| 总收益 | 170.64% | {result['total_return']:.2%} | {result['total_return']-1.7064:+.2%} |")
    lines.append(f"| 年化收益 | 16.33% | {result['annual_return']:.2%} | {result['annual_return']-0.1633:+.2%} |")
    lines.append(f"| 夏普 | 0.8442 | {result['sharpe_ratio']:.4f} | {result['sharpe_ratio']-0.8442:+.4f} |")
    lines.append(f"| 最大回撤 | -21.37% | {result['max_drawdown']:.2%} | {result['max_drawdown']-(-0.2137):+.2%} |")
    lines.append("")
    lines.append("> 注：B0.1 为历史冻结基线（2026-06-20），B0.2 关闭 momentum 因子。")
    lines.append("")
    lines.append("---")
    lines.append(f"*B0.2 config: momentum_factor_enabled={cfg['momentum_factor_enabled']}*")
    
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    
    print(f"\n  报告已保存: {report_path}")
    return result


if __name__ == '__main__':
    run_baseline()
