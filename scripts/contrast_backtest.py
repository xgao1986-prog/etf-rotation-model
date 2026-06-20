#!/usr/bin/env python3
"""
B0 新旧调仓引擎对比回测脚本（Phase 4.1 修正版）
对比旧逻辑与新逻辑（v2.5）的回测结果，数据截止至2026-06-18

使用方式：
    python scripts/contrast_backtest.py [--sample in|out|full]

输出：
    reports/contrast_report_YYYYMMDD_HHMMSS.md
    reports/contrast_detail_YYYYMMDD_HHMMSS.csv
"""

import sys
import os
import pandas as pd
import json
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from config import DB_PATH, BENCHMARK, STRATEGY_CONFIG, BACKTEST_CONFIG, build_config
from database import ETFDatabase
from strategy import StrategyEngine
from backtest import BacktestEngine

AS_OF_DATE = '2026-06-18'


def run_contrast(sample='full'):
    """运行新旧逻辑对比回测"""
    
    db = ETFDatabase()
    
    print("=" * 70)
    print(f"B0 新旧调仓引擎对比回测 (截止至 {AS_OF_DATE})")
    print("=" * 70)
    print(f"\n[1/5] 加载数据...")
    
    from config import ETF_UNIVERSE, DEFENSE_UNIVERSE
    etf_tickers = list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())
    market_df = db.get_market_data(ticker=etf_tickers)
    bench_df = db.get_market_data(ticker=BENCHMARK)
    
    if market_df.empty or bench_df.empty:
        print("[FAIL] 数据库无数据，请先运行: python main.py update --full")
        return None
    
    print(f"  行情数据: {len(market_df):,} 条")
    print(f"  标的数量: {market_df['ticker'].nunique()} 只")
    print(f"  日期范围: {market_df['date'].min()} ~ {market_df['date'].max()}")
    
    # 准备两种配置
    cfg_old = build_config()
    cfg_old['use_v2_rebalance'] = False
    cfg_old['fallback_equity_enabled'] = False
    
    cfg_new = build_config()
    cfg_new['use_v2_rebalance'] = True
    cfg_new['fallback_equity_enabled'] = False
    
    # 运行旧逻辑回测
    print(f"\n[2/5] 运行旧逻辑回测 (use_v2_rebalance=False)...")
    engine_old = BacktestEngine(cfg_old)
    if sample == 'in':
        result_old = engine_old.run_in_sample(market_df, bench_df)
    elif sample == 'out':
        result_old = engine_old.run_out_sample(market_df, bench_df)
    else:
        result_old = engine_old.run(market_df, bench_df, as_of_date=AS_OF_DATE)
    
    if 'error' in result_old:
        print(f"[FAIL] 旧逻辑回测失败: {result_old['error']}")
        return None
    print("  [OK] 完成")
    
    # 运行新逻辑回测
    print(f"\n[3/5] 运行新逻辑回测 (use_v2_rebalance=True)...")
    engine_new = BacktestEngine(cfg_new)
    if sample == 'in':
        result_new = engine_new.run_in_sample(market_df, bench_df)
    elif sample == 'out':
        result_new = engine_new.run_out_sample(market_df, bench_df)
    else:
        result_new = engine_new.run(market_df, bench_df, as_of_date=AS_OF_DATE)
    
    if 'error' in result_new:
        print(f"[FAIL] 新逻辑回测失败: {result_new['error']}")
        return None
    print("  [OK] 完成")
    
    # 对比分析
    print(f"\n[4/5] 对比分析...")
    contrast = compute_contrast(result_old, result_new)
    
    # 生成报告
    print(f"\n[5/5] 生成报告...")
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    report_path = f'reports/contrast_report_{timestamp}.md'
    detail_path = f'reports/contrast_detail_{timestamp}.csv'
    
    generate_report(contrast, result_old, result_new, report_path)
    generate_detail_csv(contrast, detail_path)
    
    print(f"\n{'='*70}")
    print("对比完成")
    print(f"  报告: {report_path}")
    print(f"  明细: {detail_path}")
    print(f"{'='*70}")
    
    print_contrast_summary(contrast)
    
    return contrast


def compute_contrast(result_old, result_new):
    """计算新旧逻辑对比指标"""
    
    metrics = [
        'total_return', 'annual_return', 'volatility', 'sharpe_ratio',
        'max_drawdown', 'num_trades', 'buy_count', 'sell_count',
        'total_commission', 'rebalance_count', 'win_rate', 'avg_holdings',
    ]
    
    contrast = {}
    for m in metrics:
        old_v = result_old.get(m, 0)
        new_v = result_new.get(m, 0)
        diff = new_v - old_v
        pct_diff = (diff / abs(old_v) * 100) if old_v != 0 else 0
        contrast[m] = {
            'old': old_v,
            'new': new_v,
            'diff': diff,
            'pct_diff': pct_diff,
        }
    
    # 调仓日对比
    old_rebalances = set(result_old.get('rebalance_dates', []))
    new_rebalances = set(result_new.get('rebalance_dates', []))
    contrast['rebalance_dates'] = {
        'old_count': len(old_rebalances),
        'new_count': len(new_rebalances),
        'common': len(old_rebalances & new_rebalances),
        'old_only': len(old_rebalances - new_rebalances),
        'new_only': len(new_rebalances - old_rebalances),
    }
    
    # 持仓对比（最终持仓）
    # 从nav_df最后一天的positions_detail提取
    old_nav = result_old.get('nav_df', pd.DataFrame())
    new_nav = result_new.get('nav_df', pd.DataFrame())
    
    old_positions = {}
    new_positions = {}
    if not old_nav.empty and 'positions_detail' in old_nav.columns:
        last_detail = old_nav['positions_detail'].iloc[-1]
        if isinstance(last_detail, dict):
            old_positions = {k: v.get('market_value', 0) for k, v in last_detail.items()}
    if not new_nav.empty and 'positions_detail' in new_nav.columns:
        last_detail = new_nav['positions_detail'].iloc[-1]
        if isinstance(last_detail, dict):
            new_positions = {k: v.get('market_value', 0) for k, v in last_detail.items()}
    
    contrast['final_positions'] = {
        'old_count': len(old_positions),
        'new_count': len(new_positions),
        'common': len(set(old_positions.keys()) & set(new_positions.keys())),
    }
    
    # 交易记录对比
    old_trades = result_old.get('trades_df', pd.DataFrame())
    new_trades = result_new.get('trades_df', pd.DataFrame())
    
    contrast['trades'] = {
        'old_count': len(old_trades),
        'new_count': len(new_trades),
        'old_buy_count': len(old_trades[old_trades['action'] == 'BUY']) if not old_trades.empty else 0,
        'new_buy_count': len(new_trades[new_trades['action'] == 'BUY']) if not new_trades.empty else 0,
        'old_sell_count': len(old_trades[old_trades['action'].isin(['SELL', 'STOP_LOSS'])]) if not old_trades.empty else 0,
        'new_sell_count': len(new_trades[new_trades['action'].isin(['SELL', 'STOP_LOSS'])]) if not new_trades.empty else 0,
    }
    
    return contrast


def print_contrast_summary(contrast):
    """打印对比摘要"""
    print(f"\n{'='*70}")
    print("关键差异摘要")
    print(f"{'='*70}")
    
    key_metrics = ['total_return', 'annual_return', 'sharpe_ratio', 'max_drawdown', 'num_trades', 'rebalance_count', 'total_commission']
    for m in key_metrics:
        if m in contrast:
            d = contrast[m]
            print(f"  {m:20s}: 旧={d['old']:10.4f}  新={d['new']:10.4f}  差异={d['diff']:+10.4f} ({d['pct_diff']:+.2f}%)")
    
    if 'rebalance_dates' in contrast:
        rd = contrast['rebalance_dates']
        print(f"\n  调仓次数对比:")
        print(f"    旧逻辑: {rd['old_count']} 次")
        print(f"    新逻辑: {rd['new_count']} 次")
        print(f"    共同: {rd['common']} 次")
        print(f"    旧独有: {rd['old_only']} 次")
        print(f"    新独有: {rd['new_only']} 次")
    
    print(f"\n{'='*70}")
    
    # 退化/改进判断
    total_return_old = contrast.get('total_return', {}).get('old', 0)
    total_return_new = contrast.get('total_return', {}).get('new', 0)
    sharpe_old = contrast.get('sharpe_ratio', {}).get('old', 0)
    sharpe_new = contrast.get('sharpe_ratio', {}).get('new', 0)
    
    if total_return_new >= total_return_old and sharpe_new >= sharpe_old:
        print("[OK] 结论: 新逻辑无退化（收益和夏普均≥旧逻辑）")
    elif total_return_new < total_return_old or sharpe_new < sharpe_old:
        print("[WARN] 结论: 新逻辑可能有退化，需进一步分析")
    else:
        print("[INFO] 结论: 收益和夏普有升有降，需具体分析")
    
    print(f"{'='*70}")


def generate_report(contrast, result_old, result_new, path):
    """生成 Markdown 对比报告"""
    
    # 获取NAV最后日期
    old_nav = result_old.get('nav_df', pd.DataFrame())
    new_nav = result_new.get('nav_df', pd.DataFrame())
    old_last_date = old_nav['date'].iloc[-1] if not old_nav.empty else 'N/A'
    new_last_date = new_nav['date'].iloc[-1] if not new_nav.empty else 'N/A'
    
    lines = [
        "# B0 新旧调仓引擎对比报告\n",
        f"\n生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n",
        f"\n**本次回测数据截止至 {AS_OF_DATE}，NAV最后日期为 {new_last_date}。**\n",
        "\n## 对比配置\n",
        "\n| 项目 | 旧逻辑 | 新逻辑 |\n",
        "|------|--------|--------|\n",
        "| 调仓引擎 | 旧版 (逐只遍历) | v2.5 (纯函数/顺序独立) |\n",
        "| 资金分配 | 逐只消耗现金 | 统一缩放/同比例 |\n",
        "| 防御资产 | 优先级写反 | 行业优先/防御填充 |\n",
        "| 槽位管理 | 共享槽位 | 行业/防御独立 |\n",
        "\n## 关键指标对比\n",
        "\n| 指标 | 旧逻辑 | 新逻辑 | 差异 | 变化% |\n",
        "|------|--------|--------|------|-------|\n",
    ]
    
    for m in ['total_return', 'annual_return', 'volatility', 'sharpe_ratio', 'max_drawdown', 'num_trades', 'buy_count', 'sell_count', 'total_commission', 'rebalance_count', 'win_rate', 'avg_holdings']:
        if m in contrast:
            d = contrast[m]
            lines.append(f"| {m} | {d['old']:.4f} | {d['new']:.4f} | {d['diff']:+.4f} | {d['pct_diff']:+.2f}% |\n")
    
    lines.extend([
        "\n## 调仓日对比\n",
        "\n| 项目 | 数值 |\n",
        "|------|------|\n",
    ])
    
    if 'rebalance_dates' in contrast:
        rd = contrast['rebalance_dates']
        lines.append(f"| 旧逻辑调仓次数 | {rd['old_count']} |\n")
        lines.append(f"| 新逻辑调仓次数 | {rd['new_count']} |\n")
        lines.append(f"| 共同调仓日 | {rd['common']} |\n")
        lines.append(f"| 旧独有调仓日 | {rd['old_only']} |\n")
        lines.append(f"| 新独有调仓日 | {rd['new_only']} |\n")
    
    # 添加调仓日期列表
    old_dates = result_old.get('rebalance_dates', [])
    new_dates = result_new.get('rebalance_dates', [])
    if old_dates:
        lines.append(f"\n旧逻辑调仓日期: {', '.join(old_dates[:10])}{'...' if len(old_dates) > 10 else ''}\n")
    if new_dates:
        lines.append(f"新逻辑调仓日期: {', '.join(new_dates[:10])}{'...' if len(new_dates) > 10 else ''}\n")
    
    # 最终NAV
    old_final_nav = old_nav['nav'].iloc[-1] if not old_nav.empty else 0
    new_final_nav = new_nav['nav'].iloc[-1] if not new_nav.empty else 0
    lines.extend([
        "\n## 最终NAV\n",
        f"\n| 逻辑 | 最终NAV |\n",
        f"|------|----------|\n",
        f"| 旧逻辑 | {old_final_nav:,.2f} |\n",
        f"| 新逻辑 | {new_final_nav:,.2f} |\n",
    ])
    
    # 结论
    total_return_old = contrast.get('total_return', {}).get('old', 0)
    total_return_new = contrast.get('total_return', {}).get('new', 0)
    sharpe_old = contrast.get('sharpe_ratio', {}).get('old', 0)
    sharpe_new = contrast.get('sharpe_ratio', {}).get('new', 0)
    
    if total_return_new >= total_return_old and sharpe_new >= sharpe_old:
        conclusion = "新逻辑无退化（收益和夏普均≥旧逻辑），可正式替换。"
    elif total_return_new < total_return_old or sharpe_new < sharpe_old:
        conclusion = "新逻辑有退化，需进一步分析后再决定是否替换。"
    else:
        conclusion = "收益和夏普有升有降，需具体分析。"
    
    lines.extend([
        "\n## 结论\n",
        f"\n{conclusion}\n",
        f"\n本次回测数据截止至 {AS_OF_DATE}，NAV最后日期为 {new_last_date}。\n",
    ])
    
    with open(path, 'w', encoding='utf-8') as f:
        f.writelines(lines)


def generate_detail_csv(contrast, path):
    """生成 CSV 明细"""
    rows = []
    for m, d in contrast.items():
        if isinstance(d, dict) and 'old' in d and 'new' in d:
            rows.append({
                'metric': m,
                'old': d['old'],
                'new': d['new'],
                'diff': d['diff'],
                'pct_diff': d['pct_diff'],
            })
    
    df = pd.DataFrame(rows)
    df.to_csv(path, index=False, encoding='utf-8-sig')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='B0 新旧调仓引擎对比回测')
    parser.add_argument('--sample', choices=['in', 'out', 'full'], default='full',
                        help='回测样本范围: in=样本内, out=样本外, full=全区间')
    args = parser.parse_args()
    
    run_contrast(args.sample)
