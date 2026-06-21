#!/usr/bin/env python3
"""
补齐数据影响受控A/B实验

A组：正常使用当前完整数据（含THS补齐的06-08~12数据）
B组：在内存中排除本次新增的THS数据，以及2026-06-08至06-12的沪深300数据，
     模拟补齐前状态。不修改数据库。

目的：判断B组是否能复现冻结基线NAV 2,809,091
"""

import sys, os, pandas as pd
import numpy as np
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, 'src'))

from config import build_config, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK
from database import ETFDatabase
from backtest import BacktestEngine

AS_OF_DATE = '2026-06-18'
B0_TICKERS = sorted(set(list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())))

# 冻结基线指标
FROZEN_BASELINE = {
    'final_nav': 2_809_091,
    'total_return_pct': 180.91,
    'annual_return_pct': 16.99,
    'sharpe': 0.8985,
    'max_drawdown_pct': 17.75,
    'trades': 801,
}


def run_group_a(market_df, bench_df):
    """A组：完整数据（含THS补齐）"""
    print("\n" + "="*70)
    print("A组：完整数据（含THS补齐）")
    print("="*70)
    
    cfg = build_config()
    cfg['fallback_equity_enabled'] = False
    cfg['momentum_factor_enabled'] = False
    cfg['volatility_factor_enabled'] = False
    
    engine = BacktestEngine(cfg)
    result = engine.run(market_df.copy(), bench_df.copy(), as_of_date=AS_OF_DATE)
    
    print(f"  最终NAV: {result['nav_df']['nav'].iloc[-1]:,.2f}")
    print(f"  总收益: {result['total_return']*100:.2f}%")
    print(f"  交易次数: {result['num_trades']}")
    
    return result


def run_group_b(market_df, bench_df):
    """B组：排除THS数据 + 排除06-08~12沪深300数据"""
    print("\n" + "="*70)
    print("B组：排除THS数据 + 排除06-08~12沪深300")
    print("="*70)
    
    # 排除THS来源的market数据
    market_df_b = market_df[market_df['source'] != 'THS'].copy()
    ths_excluded = len(market_df) - len(market_df_b)
    print(f"  排除THS记录: {ths_excluded}条")
    
    # 排除06-08~12的沪深300数据
    bench_df_b = bench_df[~(
        (bench_df['date'] >= '2026-06-08') & 
        (bench_df['date'] <= '2026-06-12')
    )].copy()
    bench_excluded = len(bench_df) - len(bench_df_b)
    print(f"  排除沪深300(06-08~12): {bench_excluded}条")
    
    # 报告06-08~12期间各ticker的数据情况
    print(f"  06-08~12数据可用性:")
    for d in ['2026-06-08', '2026-06-09', '2026-06-10', '2026-06-11', '2026-06-12']:
        day_data = market_df_b[market_df_b['date'] == d]
        present = set(day_data['ticker'].unique())
        missing = set(B0_TICKERS) - present
        bench_present = len(bench_df_b[bench_df_b['date'] == d]) > 0
        print(f"    {d}: ETF={len(present)}/18, 基准={'OK' if bench_present else 'MISSING'}")
    
    cfg = build_config()
    cfg['fallback_equity_enabled'] = False
    cfg['momentum_factor_enabled'] = False
    cfg['volatility_factor_enabled'] = False
    
    engine = BacktestEngine(cfg)
    result = engine.run(market_df_b, bench_df_b, as_of_date=AS_OF_DATE)
    
    print(f"  最终NAV: {result['nav_df']['nav'].iloc[-1]:,.2f}")
    print(f"  总收益: {result['total_return']*100:.2f}%")
    print(f"  交易次数: {result['num_trades']}")
    
    return result


def compare_groups(result_a, result_b):
    """比较两组结果"""
    print("\n" + "="*70)
    print("A/B组比较")
    print("="*70)
    
    nav_a = result_a['nav_df'][['date', 'nav']].copy()
    nav_b = result_b['nav_df'][['date', 'nav']].copy()
    
    nav_a['date'] = pd.to_datetime(nav_a['date']).dt.strftime('%Y-%m-%d')
    nav_b['date'] = pd.to_datetime(nav_b['date']).dt.strftime('%Y-%m-%d')
    
    merged = nav_a.merge(nav_b, on='date', suffixes=('_a', '_b'))
    merged['diff'] = merged['nav_a'] - merged['nav_b']
    merged['diff_pct'] = merged['diff'] / merged['nav_b'] * 100
    
    # 首次NAV分歧日期
    diverged = merged[merged['diff'].abs() > 0.01]
    if not diverged.empty:
        first_div = diverged.iloc[0]
        print(f"\n  首次NAV分歧日期: {first_div['date']}")
        print(f"    A组NAV={first_div['nav_a']:.2f}, B组NAV={first_div['nav_b']:.2f}")
        print(f"    差异={first_div['diff']:.2f} ({first_div['diff_pct']:.4f}%)")
    else:
        print(f"\n  NAV逐日一致（无分歧）")
    
    # 最终NAV差异
    final_a = merged['nav_a'].iloc[-1]
    final_b = merged['nav_b'].iloc[-1]
    print(f"\n  最终NAV:")
    print(f"    A组: {final_a:,.2f}")
    print(f"    B组: {final_b:,.2f}")
    print(f"    差异: {final_a - final_b:,.2f} ({(final_a/final_b - 1)*100:.2f}%)")
    
    # 与冻结基线比较
    print(f"\n  与冻结基线比较 (NAV={FROZEN_BASELINE['final_nav']:,}):")
    print(f"    A组偏离: {final_a - FROZEN_BASELINE['final_nav']:,.2f} ({(final_a/FROZEN_BASELINE['final_nav'] - 1)*100:.2f}%)")
    print(f"    B组偏离: {final_b - FROZEN_BASELINE['final_nav']:,.2f} ({(final_b/FROZEN_BASELINE['final_nav'] - 1)*100:.2f}%)")
    
    # 判断
    b_matches = abs(final_b - FROZEN_BASELINE['final_nav']) < 1000
    if b_matches:
        print(f"\n  === 结论: B组复现了冻结基线 (差异<{1000:.0f}) ===")
        print(f"  变化来自补齐数据")
    else:
        print(f"\n  === 结论: B组未复现冻结基线 (差异>{abs(final_b - FROZEN_BASELINE['final_nav']):,.0f}) ===")
        print(f"  差异不仅来自补齐数据，需进一步定位")
    
    # 交易记录比较
    trades_a = result_a['trades_df']
    trades_b = result_b['trades_df']
    
    print(f"\n  交易记录:")
    print(f"    A组: {len(trades_a)}笔")
    print(f"    B组: {len(trades_b)}笔")
    
    # 首笔不同交易
    if len(trades_a) > 0 and len(trades_b) > 0:
        trades_a_sorted = trades_a.sort_values(['date', 'ticker', 'action']).reset_index(drop=True)
        trades_b_sorted = trades_b.sort_values(['date', 'ticker', 'action']).reset_index(drop=True)
        
        min_len = min(len(trades_a_sorted), len(trades_b_sorted))
        first_diff_idx = None
        for i in range(min_len):
            a_row = trades_a_sorted.iloc[i]
            b_row = trades_b_sorted.iloc[i]
            if (a_row['date'] != b_row['date'] or 
                a_row['ticker'] != b_row['ticker'] or 
                a_row['action'] != b_row['action']):
                first_diff_idx = i
                break
        
        if first_diff_idx is not None:
            a_row = trades_a_sorted.iloc[first_diff_idx]
            b_row = trades_b_sorted.iloc[first_diff_idx]
            d_a = a_row['date'] if isinstance(a_row['date'], str) else a_row['date'].strftime('%Y-%m-%d')
            d_b = b_row['date'] if isinstance(b_row['date'], str) else b_row['date'].strftime('%Y-%m-%d')
            print(f"\n  首笔不同交易:")
            print(f"    A组[{first_diff_idx}]: {d_a} {a_row['ticker']} {a_row['action']}")
            print(f"    B组[{first_diff_idx}]: {d_b} {b_row['ticker']} {b_row['action']}")
        else:
            print(f"\n  前{min_len}笔交易一致")
            if len(trades_a) != len(trades_b):
                print(f"  但总笔数不同: A={len(trades_a)}, B={len(trades_b)}")
    
    # 06-08~12及之后的差异
    print(f"\n  06-08~12及之后交易:")
    june_a = trades_a[trades_a['date'] >= '2026-06-08']
    june_b = trades_b[trades_b['date'] >= '2026-06-08']
    print(f"    A组: {len(june_a)}笔")
    for _, t in june_a.iterrows():
        d = t['date'] if isinstance(t['date'], str) else t['date'].strftime('%Y-%m-%d')
        print(f"      {d} {t['ticker']:12s} {t['action']:10s}")
    print(f"    B组: {len(june_b)}笔")
    for _, t in june_b.iterrows():
        d = t['date'] if isinstance(t['date'], str) else t['date'].strftime('%Y-%m-%d')
        print(f"      {d} {t['ticker']:12s} {t['action']:10s}")
    
    # 保存NAV比较
    compare_path = os.path.join(BASE_DIR, 'reports', 'ab_test_nav_comparison.csv')
    merged[['date', 'nav_a', 'nav_b', 'diff', 'diff_pct']].to_csv(compare_path, index=False)
    print(f"\n  NAV比较已保存: {compare_path}")
    
    return merged, b_matches


def save_results(result_a, result_b, b_matches):
    """保存两组完整结果"""
    
    # 保存逐日NAV
    nav_a_path = os.path.join(BASE_DIR, 'reports', 'ab_test_nav_group_a.csv')
    nav_b_path = os.path.join(BASE_DIR, 'reports', 'ab_test_nav_group_b.csv')
    result_a['nav_df'][['date', 'nav']].to_csv(nav_a_path, index=False)
    result_b['nav_df'][['date', 'nav']].to_csv(nav_b_path, index=False)
    print(f"\n  NAV序列已保存:")
    print(f"    A组: {nav_a_path}")
    print(f"    B组: {nav_b_path}")
    
    # 保存交易记录
    trades_a_path = os.path.join(BASE_DIR, 'reports', 'ab_test_trades_group_a.csv')
    trades_b_path = os.path.join(BASE_DIR, 'reports', 'ab_test_trades_group_b.csv')
    result_a['trades_df'].to_csv(trades_a_path, index=False)
    result_b['trades_df'].to_csv(trades_b_path, index=False)
    print(f"\n  交易记录已保存:")
    print(f"    A组: {trades_a_path}")
    print(f"    B组: {trades_b_path}")
    
    # 保存汇总指标
    summary = {
        'group': ['A_完整数据', 'B_排除THS', '冻结基线'],
        'final_nav': [
            result_a['nav_df']['nav'].iloc[-1],
            result_b['nav_df']['nav'].iloc[-1],
            FROZEN_BASELINE['final_nav']
        ],
        'total_return_pct': [
            result_a['total_return'] * 100,
            result_b['total_return'] * 100,
            FROZEN_BASELINE['total_return_pct']
        ],
        'annual_return_pct': [
            result_a['annual_return'] * 100,
            result_b['annual_return'] * 100,
            FROZEN_BASELINE['annual_return_pct']
        ],
        'sharpe': [
            result_a['sharpe_ratio'],
            result_b['sharpe_ratio'],
            FROZEN_BASELINE['sharpe']
        ],
        'max_drawdown_pct': [
            abs(result_a['max_drawdown']) * 100,
            abs(result_b['max_drawdown']) * 100,
            FROZEN_BASELINE['max_drawdown_pct']
        ],
        'trades': [
            result_a['num_trades'],
            result_b['num_trades'],
            FROZEN_BASELINE['trades']
        ],
    }
    summary_df = pd.DataFrame(summary)
    summary_path = os.path.join(BASE_DIR, 'reports', 'ab_test_summary.csv')
    summary_df.to_csv(summary_path, index=False)
    print(f"\n  汇总指标已保存: {summary_path}")
    
    return summary_df


def generate_report(summary_df, merged_nav, b_matches):
    """生成A/B实验报告"""
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    lines = []
    lines.append("# 补齐数据影响受控A/B实验报告")
    lines.append("")
    lines.append(f"**实验时间**: {ts}")
    lines.append(f"**数据截止**: {AS_OF_DATE}")
    lines.append("")
    
    lines.append("## 实验设计")
    lines.append("")
    lines.append("**A组**：正常使用当前完整数据（含THS补齐的06-08~12数据）")
    lines.append("**B组**：在内存中排除THS数据 + 排除06-08~12沪深300数据，模拟补齐前状态")
    lines.append("**约束**：不修改数据库、不修改策略、不修改配置")
    lines.append("")
    
    lines.append("## 汇总指标")
    lines.append("")
    lines.append("| 指标 | A组(完整数据) | B组(排除THS) | 冻结基线 |")
    lines.append("|------|--------------|-------------|----------|")
    for _, row in summary_df.iterrows():
        lines.append(f"| {row['group']} | {row['final_nav']:,.0f} | {row['total_return_pct']:.2f}% | {row['sharpe']:.4f} |")
    lines.append("")
    
    # 重新格式化表格
    lines = lines[:-4]  # 移除错误格式的行
    lines.append("| 指标 | A组(完整数据) | B组(排除THS) | 冻结基线 |")
    lines.append("|------|--------------|-------------|----------|")
    for _, row in summary_df.iterrows():
        lines.append(f"| 最终NAV | {row['final_nav']:,.0f} | - | - |".replace("| 最终NAV |", f"| 最终NAV | {row['final_nav']:,.0f} |") if 'A_' in row['group'] else f"| 最终NAV | - | {row['final_nav']:,.0f} | - |" if 'B_' in row['group'] else f"| 最终NAV | - | - | {row['final_nav']:,.0f} |")
    
    # 更清晰的表格
    lines = lines[:-3]  # 移除错误行
    lines.append("| 指标 | A组(完整数据) | B组(排除THS) | 冻结基线 |")
    lines.append("|------|--------------|-------------|----------|")
    for col in ['final_nav', 'total_return_pct', 'annual_return_pct', 'sharpe', 'max_drawdown_pct', 'trades']:
        a_val = summary_df[summary_df['group'] == 'A_完整数据'][col].iloc[0]
        b_val = summary_df[summary_df['group'] == 'B_排除THS'][col].iloc[0]
        f_val = summary_df[summary_df['group'] == '冻结基线'][col].iloc[0]
        if col == 'final_nav':
            lines.append(f"| 最终NAV | {a_val:,.0f} | {b_val:,.0f} | {f_val:,.0f} |")
        elif col == 'trades':
            lines.append(f"| 交易次数 | {a_val:.0f} | {b_val:.0f} | {f_val:.0f} |")
        else:
            lines.append(f"| {col} | {a_val:.2f} | {b_val:.2f} | {f_val:.2f} |")
    lines.append("")
    
    lines.append("## 核心判断")
    lines.append("")
    if b_matches:
        lines.append(f"> **结论：B组复现了冻结基线**")
        lines.append(f"> B组NAV与冻结基线差异 < 1000，证明变化主要来自补齐数据。")
    else:
        b_val = summary_df[summary_df['group'] == 'B_排除THS']['final_nav'].iloc[0]
        f_val = summary_df[summary_df['group'] == '冻结基线']['final_nav'].iloc[0]
        diff = abs(b_val - f_val)
        lines.append(f"> **结论：B组未复现冻结基线**")
        lines.append(f"> B组NAV={b_val:,.0f} vs 冻结基线={f_val:,.0f}，差异={diff:,.0f}")
        lines.append(f"> 差异不仅来自补齐数据，需继续定位代码、配置或其他数据差异。")
        lines.append(f"> **不得更新基线。**")
    lines.append("")
    
    if not merged_nav.empty and 'diff' in merged_nav.columns:
        diverged = merged_nav[merged_nav['diff'].abs() > 0.01]
        if not diverged.empty:
            first = diverged.iloc[0]
            lines.append(f"**首次NAV分歧日期**: {first['date']}")
            lines.append(f"- A组NAV={first['nav_a']:.2f}, B组NAV={first['nav_b']:.2f}")
            lines.append(f"- 差异={first['diff']:.2f}")
    lines.append("")
    
    lines.append("## 06-08~12及之后交易差异")
    lines.append("")
    lines.append("> A组在06-08~12期间有数据，可以产生交易；")
    lines.append("> B组在06-08~12期间大部分ETF无数据，无法产生交易。")
    lines.append("")
    
    lines.append("## 数据文件")
    lines.append("")
    lines.append("- `reports/ab_test_nav_group_a.csv` — A组逐日NAV")
    lines.append("- `reports/ab_test_nav_group_b.csv` — B组逐日NAV")
    lines.append("- `reports/ab_test_trades_group_a.csv` — A组交易记录")
    lines.append("- `reports/ab_test_trades_group_b.csv` — B组交易记录")
    lines.append("- `reports/ab_test_summary.csv` — 汇总指标")
    lines.append("- `reports/ab_test_nav_comparison.csv` — NAV逐日比较")
    lines.append("")
    
    lines.append("---")
    lines.append(f"*实验完成。不修改生产策略。*")
    
    report_path = os.path.join(BASE_DIR, 'reports', 'ab_test_data_fill_impact.md')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    
    print(f"\n{'='*70}")
    print(f"报告已生成: {report_path}")
    print(f"{'='*70}")
    
    return report_path


def main():
    print("=" * 70)
    print("补齐数据影响受控A/B实验")
    print("=" * 70)
    
    db = ETFDatabase()
    tickers = B0_TICKERS
    
    print("\n[准备] 加载数据...")
    market_df = db.get_market_data(ticker=tickers, start_date='2019-01-01', end_date=AS_OF_DATE)
    bench_df = db.get_market_data(ticker=BENCHMARK, start_date='2019-01-01', end_date=AS_OF_DATE)
    
    # 确保source列存在
    if 'source' not in market_df.columns:
        market_df['source'] = 'unknown'
    if 'source' not in bench_df.columns:
        bench_df['source'] = 'unknown'
    
    print(f"  market_df: {len(market_df)} 条")
    print(f"  bench_df: {len(bench_df)} 条")
    
    # A组
    result_a = run_group_a(market_df, bench_df)
    
    # B组
    result_b = run_group_b(market_df, bench_df)
    
    # 比较
    merged_nav, b_matches = compare_groups(result_a, result_b)
    
    # 保存
    summary_df = save_results(result_a, result_b, b_matches)
    
    # 报告
    report_path = generate_report(summary_df, merged_nav, b_matches)
    
    print(f"\n实验完成。")
    print(f"  B组复现基线: {'是' if b_matches else '否'}")
    if not b_matches:
        print(f"  警告：B组未复现冻结基线，差异不仅来自补齐数据。")
    
    return b_matches


if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
