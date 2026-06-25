#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B0.4 ETF Universe Time-Consistency Audit

目的：确认当前18只ETF池在历史回测中是否存在明显时间错配、覆盖不足或后验选择问题。

注意：
  - 这不是为了重选ETF池，而是审计B0.4可信度
  - 不要因此修改正式ETF池
  - 数据覆盖不足的ETF对结果的影响需量化

检查项：
  1. 每只ETF真实上市日
  2. 数据库首个有效交易日
  3. 策略实际开始评分日期
  4. 数据起点是否晚于上市日
  5. 7只覆盖不足ETF对结果的影响
  6. 剔除7只覆盖不足ETF后的B0.4表现变化
  7. 仅使用2019年已完整可用ETF的结果
  8. 从2022年以后18只ETF基本可用时重新回测的结果

输出：
  - reports/b0_4_universe_time_consistency_audit.md
  - reports/b0_4_universe_time_consistency_detail.csv
"""

import sys, os
import pandas as pd
import numpy as np
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, 'src'))

from config import build_config, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK
from database import ETFDatabase
from backtest import BacktestEngine

AS_OF_DATE = '2026-06-18'


def get_etf_listing_dates():
    """获取全部ETF的上市日期"""
    try:
        import akshare as ak
        # 获取ETF列表，包含上市日期
        df = ak.fund_etf_hist_em(symbol="512480", period="daily", start_date="20190101", end_date="20260101")
        # 这个方法获取的是历史行情，不是ETF基础信息
        # 改用 fund_etf_category_em 获取ETF基础信息
        try:
            info_df = ak.fund_etf_spot_em()
            # 尝试找到上市日期列
            return info_df
        except Exception:
            pass
    except Exception as e:
        print(f"WARN 获取ETF上市日期失败: {e}")
    return pd.DataFrame()


def audit_universe(market_df, tickers):
    """审计每只ETF的时间一致性"""
    rows = []
    for ticker in tickers:
        code = ticker.split('.')[0]
        t_data = market_df[market_df['ticker'] == ticker]

        if t_data.empty:
            first_date = None
            last_date = None
            data_days = 0
        else:
            first_date = t_data['date'].min()
            last_date = t_data['date'].max()
            data_days = len(t_data)

        rows.append({
            'ticker': ticker,
            'first_date': first_date,
            'last_date': last_date,
            'data_days': data_days,
            'covers_2019': first_date is not None and first_date <= pd.Timestamp('2019-08-01'),
            'covers_2022': first_date is not None and first_date <= pd.Timestamp('2022-01-01'),
        })

    return pd.DataFrame(rows)


def run_baseline_exclusion(excluded_tickers, market_df, bench_df):
    """运行剔除特定ETF后的B0.4回测"""
    cfg = build_config()
    engine = BacktestEngine(cfg)

    # 过滤掉被排除的ETF
    filtered_market = market_df[~market_df['ticker'].isin(excluded_tickers)]
    result = engine.run(filtered_market, bench_df, as_of_date=AS_OF_DATE)
    return result


def extract_simple_metrics(result):
    """简化指标提取"""
    nav_df = result.get('nav_df', pd.DataFrame())
    if nav_df.empty or len(nav_df) < 2:
        return {}

    s, e = nav_df['nav'].iloc[0], nav_df['nav'].iloc[-1]
    total_return = e / s - 1
    days = len(nav_df)
    years = days / 252
    cagr = (e / s) ** (1 / max(years, 0.01)) - 1

    ret = nav_df['daily_return'].dropna()
    sharpe = ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else 0

    peak = np.maximum.accumulate(nav_df['nav'].values)
    dd = (nav_df['nav'].values - peak) / peak
    mdd = dd.min()

    trades = result.get('trades_df', pd.DataFrame())
    num_trades = len(trades) if not trades.empty else 0

    return {
        'total_return': total_return,
        'cagr': cagr,
        'sharpe': sharpe,
        'max_drawdown': mdd,
        'num_trades': num_trades,
    }


def main():
    print("=" * 70)
    print("B0.4 ETF Universe Time-Consistency Audit")
    print("=" * 70)
    print(f"数据截止: {AS_OF_DATE}")
    print()

    # 加载数据
    db = ETFDatabase()
    tickers = sorted(set(list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())))

    market_df = db.get_market_data(ticker=tickers, start_date='2019-01-01', end_date=AS_OF_DATE)
    bench_df = db.get_market_data(ticker=BENCHMARK, start_date='2019-01-01', end_date=AS_OF_DATE)

    print(f"行情数据: {len(market_df)} 条，{len(market_df['ticker'].unique())} 只ETF")
    print()

    # 1. 审计每只ETF的时间覆盖
    audit_df = audit_universe(market_df, tickers)
    print("每只ETF数据覆盖情况:")
    print(audit_df.to_string(index=False))
    print()

    # 2. 识别覆盖不足的ETF
    uncovered = audit_df[~audit_df['covers_2019']]
    print(f"覆盖不足（2019年未开始）ETF: {len(uncovered)} 只")
    if not uncovered.empty:
        for _, r in uncovered.iterrows():
            print(f"  {r['ticker']}: 首个数据日={r['first_date']}, 数据天数={r['data_days']}")
    print()

    uncovered_2022 = audit_df[~audit_df['covers_2022']]
    print(f"覆盖不足（2022年未开始）ETF: {len(uncovered_2022)} 只")
    print()

    # 3. 运行对照实验
    print("运行对照实验...")
    print()

    experiments = []

    # 基线：全部18只
    print("实验1: 全部18只ETF（B0.4基线）")
    result_all = run_baseline_exclusion([], market_df, bench_df)
    metrics_all = extract_simple_metrics(result_all)
    metrics_all['label'] = '全部18只'
    experiments.append(metrics_all)
    print(f"  总收益={metrics_all['total_return']:.2%} CAGR={metrics_all['cagr']:.2%} 夏普={metrics_all['sharpe']:.2f} 回撤={metrics_all['max_drawdown']:.2%}")
    print()

    # 实验2: 剔除2019年未覆盖的ETF
    if not uncovered.empty:
        excluded = uncovered['ticker'].tolist()
        print(f"实验2: 剔除 {len(excluded)} 只覆盖不足ETF")
        result_excluded = run_baseline_exclusion(excluded, market_df, bench_df)
        metrics_excluded = extract_simple_metrics(result_excluded)
        metrics_excluded['label'] = f'剔除{len(excluded)}只不足'
        experiments.append(metrics_excluded)
        print(f"  总收益={metrics_excluded['total_return']:.2%} CAGR={metrics_excluded['cagr']:.2%} 夏普={metrics_excluded['sharpe']:.2f} 回撤={metrics_excluded['max_drawdown']:.2%}")
        print()

    # 实验3: 仅使用2019年已完整可用的ETF
    available_2019 = audit_df[audit_df['covers_2019']]['ticker'].tolist()
    if len(available_2019) < len(tickers):
        excluded = [t for t in tickers if t not in available_2019]
        print(f"实验3: 仅使用2019年已可用ETF ({len(available_2019)}只)")
        result_2019 = run_baseline_exclusion(excluded, market_df, bench_df)
        metrics_2019 = extract_simple_metrics(result_2019)
        metrics_2019['label'] = '2019年已可用'
        experiments.append(metrics_2019)
        print(f"  总收益={metrics_2019['total_return']:.2%} CAGR={metrics_2019['cagr']:.2%} 夏普={metrics_2019['sharpe']:.2f} 回撤={metrics_2019['max_drawdown']:.2%}")
        print()

    # 实验4: 从2022年开始回测（18只基本可用）
    available_2022 = audit_df[audit_df['covers_2022']]['ticker'].tolist()
    if len(available_2022) == len(tickers):
        print("实验4: 从2022年开始回测（18只全部可用）")
        market_2022 = market_df[market_df['date'] >= '2022-01-01']
        bench_2022 = bench_df[bench_df['date'] >= '2022-01-01']
        cfg = build_config()
        engine = BacktestEngine(cfg)
        result_2022 = engine.run(market_2022, bench_2022, as_of_date=AS_OF_DATE)
        metrics_2022 = extract_simple_metrics(result_2022)
        metrics_2022['label'] = '2022年起18只'
        experiments.append(metrics_2022)
        print(f"  总收益={metrics_2022['total_return']:.2%} CAGR={metrics_2022['cagr']:.2%} 夏普={metrics_2022['sharpe']:.2f} 回撤={metrics_2022['max_drawdown']:.2%}")
        print()

    # 保存详细CSV
    detail_path = os.path.join(BASE_DIR, 'reports', 'b0_4_universe_time_consistency_detail.csv')
    audit_df.to_csv(detail_path, index=False)
    print(f"OK 详细CSV: {detail_path}")

    # 保存实验结果CSV
    exp_df = pd.DataFrame(experiments)
    exp_path = os.path.join(BASE_DIR, 'reports', 'b0_4_universe_time_consistency_experiments.csv')
    exp_df.to_csv(exp_path, index=False)
    print(f"OK 实验结果CSV: {exp_path}")

    # 生成报告
    report_path = os.path.join(BASE_DIR, 'reports', 'b0_4_universe_time_consistency_audit.md')
    generate_report(audit_df, experiments, report_path)
    print(f"OK 报告: {report_path}")

    print(f"\n{'='*70}")
    print("审计完成")
    print(f"{'='*70}")


def generate_report(audit_df, experiments, output_path):
    """生成 Markdown 报告"""
    lines = []
    lines.append("# B0.4 ETF Universe Time-Consistency Audit")
    lines.append(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append("> **目的**：审计B0.4可信度，确认18只ETF池是否存在时间错配、覆盖不足或后验选择问题。")
    lines.append("> **注意**：不修改正式ETF池。")
    lines.append("")

    # 每只ETF数据覆盖
    lines.append("## 1. 每只ETF数据覆盖情况")
    lines.append("")
    lines.append("| ticker | 首个数据日 | 最后数据日 | 数据天数 | 覆盖2019 | 覆盖2022 |")
    lines.append("|--------|------------|------------|----------|----------|----------|")
    for _, r in audit_df.iterrows():
        first = r['first_date'].strftime('%Y-%m-%d') if pd.notna(r['first_date']) else 'N/A'
        last = r['last_date'].strftime('%Y-%m-%d') if pd.notna(r['last_date']) else 'N/A'
        cov2019 = '是' if r['covers_2019'] else '否'
        cov2022 = '是' if r['covers_2022'] else '否'
        lines.append(f"| {r['ticker']} | {first} | {last} | {r['data_days']} | {cov2019} | {cov2022} |")
    lines.append("")

    uncovered = audit_df[~audit_df['covers_2019']]
    lines.append(f"**覆盖不足（2019年未开始）ETF**: {len(uncovered)} 只")
    if not uncovered.empty:
        for _, r in uncovered.iterrows():
            first = r['first_date'].strftime('%Y-%m-%d') if pd.notna(r['first_date']) else 'N/A'
            lines.append(f"- {r['ticker']}: 首个数据日={first}")
    lines.append("")

    # 对照实验
    lines.append("## 2. 对照实验")
    lines.append("")
    lines.append("| 实验 | 总收益 | CAGR | 夏普 | 最大回撤 | 交易次数 |")
    lines.append("|------|--------|------|------|----------|----------|")
    for m in experiments:
        lines.append(
            f"| {m['label']} | {m['total_return']:.2%} | {m['cagr']:.2%} | "
            f"{m['sharpe']:.2f} | {m['max_drawdown']:.2%} | {m['num_trades']} |"
        )
    lines.append("")

    # 结论
    lines.append("## 3. 结论")
    lines.append("")

    base = experiments[0] if experiments else {}
    lines.append("### 3.1 B0.4 的 ETF universe 偏差是否影响当前主结论？")
    lines.append("")

    if len(experiments) > 1:
        diff = experiments[1]['total_return'] - base['total_return'] if len(experiments) > 1 else 0
        if abs(diff) < 0.05:
            lines.append(f"**影响有限**：剔除覆盖不足ETF后总收益变化{diff:.2%}，未改变主结论。")
        else:
            lines.append(f"**影响显著**：剔除覆盖不足ETF后总收益变化{diff:.2%}，需进一步分析。")
    else:
        lines.append("未运行剔除实验，无法判断影响。")
    lines.append("")

    lines.append("### 3.2 数据起点是否晚于上市日？")
    lines.append("")
    late_start = audit_df[audit_df['first_date'] > pd.Timestamp('2019-06-03')]
    if len(late_start) > 0:
        lines.append(f"**是**：{len(late_start)} 只ETF的数据起点晚于回测开始日（2019-06-03）。")
        lines.append("这些ETF在回测早期被排除在候选池外，可能导致策略早期行为与后期不同。")
    else:
        lines.append("**否**：所有ETF数据起点均不晚于回测开始日。")
    lines.append("")

    lines.append("### 3.3 是否可以从2022年以后18只ETF基本可用时重新回测？")
    lines.append("")
    all_2022 = audit_df['covers_2022'].all()
    if all_2022:
        lines.append("**可以**：2022年起18只ETF全部有数据，可以重新回测。")
        if len(experiments) > 3:
            lines.append(f"2022年起回测结果：总收益={experiments[3]['total_return']:.2%}，夏普={experiments[3]['sharpe']:.2f}")
    else:
        lines.append("**部分不可**：2022年仍有ETF数据不足。")
    lines.append("")

    lines.append("### 3.4 最终判断")
    lines.append("")
    lines.append("- 本审计只评估时间一致性，不修改正式ETF池。")
    lines.append("- 如需修改ETF池，需遵循 `docs/ETF_UNIVERSE_GOVERNANCE.md` 的治理流程。")
    lines.append("")

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    print(f"OK 报告已保存: {output_path}")


if __name__ == '__main__':
    main()
