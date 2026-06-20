#!/usr/bin/env python3
"""
Phase 5.7: B0.3 基准回测 + 与 B0.2 精确对比

运行B0.3（volatility_factor_enabled=False）完整回测，
并与B0.2（旧逻辑，vol_score≈0）进行逐日/逐笔精确对比。

预期：B0.3 == B0.2（完全一致）
如果任何差异，停止验收。

输出：
- reports/baseline_B0.3_时间戳.md
- reports/b0_2_vs_b0_3_时间戳.md
"""

import sys, os
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, 'src'))

import pandas as pd
import numpy as np
from datetime import datetime
from copy import deepcopy

from config import build_config, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK
from database import ETFDatabase
from backtest import BacktestEngine

AS_OF_DATE = '2026-06-18'


def run_baseline(cfg, label):
    """运行回测并返回结果"""
    db = ETFDatabase()
    tickers = sorted(set(list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())))
    market_df = db.get_market_data(ticker=tickers, start_date='2019-01-01', end_date=AS_OF_DATE)
    bench_df = db.get_market_data(ticker=BENCHMARK, start_date='2019-01-01', end_date=AS_OF_DATE)
    
    engine = BacktestEngine(cfg)
    result = engine.run(market_df, bench_df, as_of_date=AS_OF_DATE)
    return result


def compare_results(b0_2_result, b0_3_result):
    """精确对比B0.2和B0.3的回测结果"""
    diffs = []
    
    # 1. 核心指标对比
    metrics_to_compare = [
        ('total_return', '总收益'),
        ('annual_return', '年化收益'),
        ('sharpe_ratio', '夏普'),
        ('max_drawdown', '最大回撤'),
        ('num_trades', '交易次数'),
        ('buy_count', '买入次数'),
        ('sell_count', '卖出次数'),
        ('rebalance_count', '调仓次数'),
    ]
    
    for key, label in metrics_to_compare:
        v2 = b0_2_result[key]
        v3 = b0_3_result[key]
        if v2 != v3:
            diffs.append(f"  {label}: B0.2={v2}, B0.3={v3}, delta={v3-v2}")
    
    # 2. 最终NAV对比
    nav2 = b0_2_result['nav_df']
    nav3 = b0_3_result['nav_df']
    
    final_nav2 = nav2['nav'].iloc[-1]
    final_nav3 = nav3['nav'].iloc[-1]
    if final_nav2 != final_nav3:
        diffs.append(f"  最终NAV: B0.2={final_nav2:,.0f}, B0.3={final_nav3:,.0f}, delta={final_nav3-final_nav2:,.0f}")
    
    # 3. NAV逐日序列对比
    merged_nav = nav2[['date', 'nav']].merge(nav3[['date', 'nav']], on='date', suffixes=('_b02', '_b03'))
    nav_mismatch = merged_nav[merged_nav['nav_b02'] != merged_nav['nav_b03']]
    if len(nav_mismatch) > 0:
        diffs.append(f"  NAV逐日: {len(nav_mismatch)} 天不一致")
        diffs.append(f"    首次差异: {nav_mismatch['date'].iloc[0]} B0.2={nav_mismatch['nav_b02'].iloc[0]:.2f} B0.3={nav_mismatch['nav_b03'].iloc[0]:.2f}")
    
    # 4. 交易明细对比
    trades2 = b0_2_result['trades_df']
    trades3 = b0_3_result['trades_df']
    
    if len(trades2) != len(trades3):
        diffs.append(f"  交易数量: B0.2={len(trades2)}, B0.3={len(trades3)}")
    else:
        # 逐笔对比（按date+action+ticker排序）
        if not trades2.empty and not trades3.empty:
            cols_to_compare = ['date', 'action', 'ticker', 'shares', 'price', 'pnl_pct', 'commission']
            available_cols = [c for c in cols_to_compare if c in trades2.columns and c in trades3.columns]
            if available_cols:
                t2 = trades2[available_cols].sort_values(['date', 'action', 'ticker']).reset_index(drop=True)
                t3 = trades3[available_cols].sort_values(['date', 'action', 'ticker']).reset_index(drop=True)
                if not t2.equals(t3):
                    diffs.append(f"  交易明细: 逐笔不匹配")
    
    return diffs


def main():
    print("=" * 70)
    print("Phase 5.7: B0.3 Baseline + Exact Comparison with B0.2")
    print("=" * 70)
    
    # B0.2 config: 旧逻辑（vol_score由calculate_scores计算，几乎恒为0）
    # 为了复现B0.2，我们需要使用旧逻辑：不设置volatility_factor_enabled，让它走旧代码路径
    # 但旧代码中 calculate_scores 已经计算了 vol_score，只是阈值错误导致几乎为0
    # 要精确复现B0.2，我们需要：momentum=False, volatility=旧逻辑（阈值计算，结果≈0）
    
    # 实际上，B0.2的config是：momentum_factor_enabled=False, 没有volatility_factor_enabled开关
    # 所以旧逻辑走 calculate_scores 中的 vol_score 计算（阈值[0.01,0.04]/(0.04,0.06]，结果≈0）
    
    # 为了对比，我们需要：
    # B0.2: momentum=False, 走旧vol_score逻辑（≈0）
    # B0.3: momentum=False, volatility=False（显式关闭，结果=0）
    
    # 构建B0.2配置（复现旧逻辑）
    cfg_b02 = build_config()
    cfg_b02['fallback_equity_enabled'] = False
    cfg_b02['momentum_factor_enabled'] = False
    # 删除volatility_factor_enabled以模拟旧逻辑（如果config中有的话）
    if 'volatility_factor_enabled' in cfg_b02:
        del cfg_b02['volatility_factor_enabled']
    
    # 构建B0.3配置（新逻辑：显式关闭）
    cfg_b03 = build_config()
    cfg_b03['fallback_equity_enabled'] = False
    cfg_b03['momentum_factor_enabled'] = False
    cfg_b03['volatility_factor_enabled'] = False
    
    print(f"\nB0.2 config: momentum=False, vol=old logic (broken thresholds)")
    print(f"B0.3 config: momentum=False, volatility_factor_enabled=False")
    
    # 运行两个回测
    print("\n[1/3] Running B0.2 (old logic)...")
    result_b02 = run_baseline(cfg_b02, "B0.2")
    
    print("\n[2/3] Running B0.3 (explicit off)...")
    result_b03 = run_baseline(cfg_b03, "B0.3")
    
    # 精确对比
    print("\n[3/3] Exact comparison...")
    diffs = compare_results(result_b02, result_b03)
    
    if diffs:
        print("\n" + "=" * 70)
        print("ERROR: B0.3 != B0.2, differences found:")
        for d in diffs:
            print(d)
        print("=" * 70)
        print("\nSTOP: B0.3 acceptance FAILED. Investigation required.")
        return False
    else:
        print("\n" + "=" * 70)
        print("SUCCESS: B0.3 == B0.2 (exact match)")
        print("=" * 70)
    
    # 生成报告
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    b03_report = os.path.join('reports', f'baseline_B0.3_{ts}.md')
    compare_report = os.path.join('reports', f'b0_2_vs_b0_3_{ts}.md')
    
    # B0.3基准报告
    nav = result_b03['nav_df']
    lines = []
    lines.append("# B0.3 基准回测报告")
    lines.append("")
    lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**数据截止**: {AS_OF_DATE}")
    lines.append("")
    lines.append("## 配置")
    lines.append("")
    lines.append(f"| 参数 | 值 |")
    lines.append(f"|------|-----|")
    lines.append(f"| momentum_factor_enabled | False |")
    lines.append(f"| volatility_factor_enabled | False |")
    lines.append(f"| use_v2_rebalance | {cfg_b03['use_v2_rebalance']} |")
    lines.append(f"| rebalance_weekday | {cfg_b03['rebalance_weekday']} |")
    lines.append(f"| stop_loss | {cfg_b03['stop_loss']:.0%} |")
    lines.append(f"| max_position_per_etf | {cfg_b03['max_position_per_etf']:.0%} |")
    lines.append("")
    lines.append("## 核心指标")
    lines.append("")
    lines.append(f"| 指标 | B0.3 |")
    lines.append(f"|------|------|")
    lines.append(f"| 最终NAV | {nav['nav'].iloc[-1]:,.0f} |")
    lines.append(f"| 总收益 | {result_b03['total_return']:.2%} |")
    lines.append(f"| 年化收益 | {result_b03['annual_return']:.2%} |")
    lines.append(f"| 夏普比率 | {result_b03['sharpe_ratio']:.4f} |")
    lines.append(f"| 最大回撤 | {result_b03['max_drawdown']:.2%} |")
    lines.append(f"| 交易次数 | {result_b03['num_trades']} |")
    lines.append(f"| 买入次数 | {result_b03['buy_count']} |")
    lines.append(f"| 卖出次数 | {result_b03['sell_count']} |")
    lines.append(f"| 调仓次数 | {result_b03['rebalance_count']} |")
    lines.append("")
    lines.append("---")
    lines.append(f"*B0.3 config: momentum_factor_enabled=False, volatility_factor_enabled=False*")
    
    with open(b03_report, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    
    # 对比报告
    lines2 = []
    lines2.append("# B0.2 vs B0.3 精确对比报告")
    lines2.append("")
    lines2.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines2.append("")
    lines2.append("## 对比结果")
    lines2.append("")
    if diffs:
        lines2.append("**状态**: ❌ 发现差异")
        lines2.append("")
        lines2.append("差异列表:")
        for d in diffs:
            lines2.append(f"- {d}")
    else:
        lines2.append("**状态**: ✅ 完全一致")
        lines2.append("")
        lines2.append("B0.3 与 B0.2 在所有维度上完全匹配：")
        lines2.append("- 最终NAV")
        lines2.append("- 总收益")
        lines2.append("- 年化收益")
        lines2.append("- 夏普")
        lines2.append("- 最大回撤")
        lines2.append("- 交易次数")
        lines2.append("- 交易明细")
        lines2.append("- NAV逐日序列")
    lines2.append("")
    lines2.append("## 指标对比表")
    lines2.append("")
    lines2.append("| 指标 | B0.2 | B0.3 | Delta |")
    lines2.append("|------|------|------|-------|")
    lines2.append(f"| 最终NAV | {result_b02['nav_df']['nav'].iloc[-1]:,.0f} | {result_b03['nav_df']['nav'].iloc[-1]:,.0f} | 0 |")
    lines2.append(f"| 总收益 | {result_b02['total_return']:.2%} | {result_b03['total_return']:.2%} | 0 |")
    lines2.append(f"| 年化收益 | {result_b02['annual_return']:.2%} | {result_b03['annual_return']:.2%} | 0 |")
    lines2.append(f"| 夏普 | {result_b02['sharpe_ratio']:.4f} | {result_b03['sharpe_ratio']:.4f} | 0 |")
    lines2.append(f"| 最大回撤 | {result_b02['max_drawdown']:.2%} | {result_b03['max_drawdown']:.2%} | 0 |")
    lines2.append(f"| 交易次数 | {result_b02['num_trades']} | {result_b03['num_trades']} | 0 |")
    lines2.append(f"| 买入次数 | {result_b02['buy_count']} | {result_b03['buy_count']} | 0 |")
    lines2.append(f"| 卖出次数 | {result_b02['sell_count']} | {result_b03['sell_count']} | 0 |")
    lines2.append(f"| 调仓次数 | {result_b02['rebalance_count']} | {result_b03['rebalance_count']} | 0 |")
    lines2.append("")
    lines2.append("---")
    lines2.append("*B0.2 = 旧逻辑（vol_score由calculate_scores计算，阈值错误导致≈0）*")
    lines2.append("*B0.3 = 显式关闭vol_score（volatility_factor_enabled=False）*")
    
    with open(compare_report, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines2))
    
    print(f"\n  B0.3 report: {b03_report}")
    print(f"  Compare report: {compare_report}")
    
    return True


if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
