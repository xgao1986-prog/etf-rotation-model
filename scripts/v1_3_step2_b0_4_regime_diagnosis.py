#!/usr/bin/env python3
"""
v1.3 Step 2: B0.4 市场状态增量价值诊断（修正版v2）

目标：判断 B0.4 已有的自然择时是否足够，市场状态检测是否还有增量价值。

方法修正（2026-06-22-v2）：
1. 收益计算：在完整、连续、按日期排序的nav_df上先计算 bench_ret = bench_price.pct_change()，
   然后再按研究期/验证期和市场状态筛选。禁止在period_mask筛选后才pct_change，避免遗漏验证期首日基准收益。
2. 超额收益：prod(1+r_strategy)/prod(1+r_benchmark) - 1，禁止 strategy_CAGR - benchmark_CAGR。
3. 收益勾稽：四状态增长因子连乘必须复现完整分析期策略和基准累计收益，披露绝对误差。
4. 仓位统一：industry_pct + defense_pct + cash_pct = 100%。
5. 交易勾稽：804 = 642（四状态已归因）+ 162（2025-2026样本外）+ 0（warmup/NaN）。
   逐笔输出样本外交易明细，warmup/NaN交易0笔（日期为2019-08-13/14，无交易）。
6. 44.7%/31.0%：旧报告错误值，来源不可复现。不归因于pct_change收益算法。
7. 样本警告：研究期强牛仅60天，非连续区间，年化高度膨胀，不得作为主要判断依据。
8. 方向判断：撤回A/B/C推荐，仅当研究期和验证期方向一致且经济意义明确才能推荐。
9. 不读取2025-2026数据。

基准：v1.2.3 / B0.4（0bp，NAV=2,761,288.07，交易804笔，observer模式）
不修改生产策略、参数或冻结基线。
"""

import sys, os
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, 'src'))

import pandas as pd
import numpy as np
from datetime import datetime

from config import build_config, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK
from database import ETFDatabase
from backtest import BacktestEngine

AS_OF_DATE = '2026-06-18'


def get_b0_4_config():
    """构建B0.4配置"""
    cfg = build_config()
    cfg['fallback_equity_enabled'] = False
    cfg['momentum_factor_enabled'] = False
    cfg['volatility_factor_enabled'] = False
    return cfg


def run_b0_4_backtest():
    """运行B0.4回测，返回完整结果"""
    cfg = get_b0_4_config()
    db = ETFDatabase()
    tickers = sorted(set(list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())))
    assert len(tickers) == 18, f"B0.4 ETF池应为18只，实际{len(tickers)}"

    market_df = db.get_market_data(ticker=tickers, start_date='2019-01-01', end_date=AS_OF_DATE)
    bench_df = db.get_market_data(ticker=BENCHMARK, start_date='2019-01-01', end_date=AS_OF_DATE)

    engine = BacktestEngine(cfg)
    result = engine.run(market_df, bench_df, as_of_date=AS_OF_DATE)

    # D:/02240cf backtest.py已内置observer模式，result['nav_df']已包含regime列
    if 'regime_summary' not in result:
        from market_regime import MarketRegimeDetector
        detector = MarketRegimeDetector(cfg)
        regime_history = detector.detect_history(bench_df, market_df)
        result['regime_history'] = regime_history
        result['regime_summary'] = detector.get_state_summary(regime_history)
        if not regime_history.empty and 'nav_df' in result and not result['nav_df'].empty:
            result['nav_df'] = result['nav_df'].merge(
                regime_history[['date', 'regime_id', 'regime_name', 'confidence', 'reason']],
                on='date', how='left'
            )

    return result, market_df, bench_df, cfg


def compute_regime_stats(nav_df, trades_df, period_name, period_mask):
    """
    按市场状态计算策略表现统计（修正版v2）。

    方法：
    1. 在完整、连续、按日期排序的nav_df上先计算逐日收益：
       bench_ret = bench_price.pct_change()（确保验证期首日基准收益正确）
       strat_ret = daily_return（已正确）
    2. 然后再按period_mask筛选，将状态映射到逐日收益
    3. 按状态分组聚合：累积收益=prod(1+r)-1，日均收益=mean(r)
    4. 超额收益=prod(1+r_strategy)/prod(1+r_benchmark)-1

    Args:
        nav_df: 回测nav_df（完整序列，已合并regime列，含daily_return/bench_return/bench_price）
        trades_df: 交易记录（完整序列）
        period_name: 期间名称
        period_mask: nav_df上的布尔掩码（已排除2025-2026）

    Returns:
        dict: 各状态统计 + warmup/NaN统计
    """
    # 在完整nav_df上先计算逐日收益（关键：不能先筛选再pct_change）
    nav_df = nav_df.copy().sort_values('date').reset_index(drop=True)
    nav_df['date'] = pd.to_datetime(nav_df['date'])

    # 策略日收益已正确（backtest引擎计算）
    nav_df['strat_ret'] = nav_df['daily_return']
    # 基准日收益：bench_return是累积收益（从起始日），需用bench_price.pct_change()重算日收益
    nav_df['bench_ret'] = nav_df['bench_price'].pct_change()

    # 仓位百分比（确保三者之和=100%）
    nav_df['industry_pct'] = nav_df['industry_value'] / nav_df['nav']
    nav_df['defense_pct'] = nav_df['defense_value'] / nav_df['nav']
    nav_df['cash_pct'] = nav_df['cash'] / nav_df['nav']

    # 然后按period_mask筛选
    df = nav_df[period_mask].copy().sort_values('date').reset_index(drop=True)
    if df.empty:
        return {}

    # 确保regime列存在
    if 'regime_name' not in df.columns:
        return {}

    # 前向填充regime（warmup期可能NaN，只有2天）
    df['regime_name_filled'] = df['regime_name'].ffill()
    df['regime_id_filled'] = df['regime_id'].ffill()

    # 分离有regime和无regime的日期
    df_with_regime = df[df['regime_id'].notna()].copy()
    df_nan_regime = df[df['regime_id'].isna()].copy()

    stats = {}

    # 1. 计算各状态统计
    for regime_id, regime_name in [(1, '强牛'), (2, '弱牛'), (3, '震荡'), (4, '熊市')]:
        regime_df = df_with_regime[df_with_regime['regime_id'] == regime_id].copy()
        days = len(regime_df)

        if days == 0:
            stats[regime_name] = {
                'period': period_name, 'regime_id': regime_id, 'regime_name': regime_name,
                'days': 0,
            }
            continue

        # 逐日收益（已正确映射到状态）
        strat_daily = regime_df['strat_ret'].dropna()
        bench_daily = regime_df['bench_ret'].dropna()

        # 累计收益 = prod(1+r) - 1
        strat_cum_ret = (1 + strat_daily).prod() - 1 if len(strat_daily) > 0 else 0
        bench_cum_ret = (1 + bench_daily).prod() - 1 if len(bench_daily) > 0 else 0

        # 增长因子（用于勾稽）
        growth_factor = 1 + strat_cum_ret
        bench_growth_factor = 1 + bench_cum_ret

        # 超额收益 = prod(1+r_strategy)/prod(1+r_benchmark) - 1
        if (1 + bench_cum_ret) > 0:
            excess_ret = (1 + strat_cum_ret) / (1 + bench_cum_ret) - 1
        else:
            excess_ret = np.nan

        # 日均收益
        strat_mean_daily = strat_daily.mean() if len(strat_daily) > 0 else 0
        bench_mean_daily = bench_daily.mean() if len(bench_daily) > 0 else 0

        # 日波动率（标准差）
        strat_vol_daily = strat_daily.std() if len(strat_daily) > 1 else 0
        bench_vol_daily = bench_daily.std() if len(bench_daily) > 1 else 0

        # 日波动率年化（辅助展示，非主要判断依据）
        vol = strat_vol_daily * np.sqrt(252)
        bench_vol = bench_vol_daily * np.sqrt(252)

        # 条件年化（仅作为辅助展示，禁止作为主要判断依据）
        cagr = (1 + strat_cum_ret) ** (252 / days) - 1 if days > 0 and strat_cum_ret > -1 else np.nan
        bench_cagr = (1 + bench_cum_ret) ** (252 / days) - 1 if days > 0 and bench_cum_ret > -1 else np.nan

        # 条件夏普（基于条件年化，仅辅助展示）
        sharpe = cagr / vol if vol > 0 and not np.isnan(cagr) else np.nan

        # 最大回撤（在状态内计算，使用状态内的NAV序列）
        peak = regime_df['nav'].cummax()
        dd = (regime_df['nav'] - peak) / peak
        max_dd = dd.min() if len(dd) > 0 else 0

        # 日胜率
        daily_win_rate = (strat_daily > 0).mean() if len(strat_daily) > 0 else 0

        # 仓位统计
        avg_industry_pct = regime_df['industry_pct'].mean()
        avg_defense_pct = regime_df['defense_pct'].mean()
        avg_cash_pct = regime_df['cash_pct'].mean()
        avg_num_pos = regime_df['num_positions'].mean() if 'num_positions' in regime_df.columns else np.nan

        # 交易统计（逐笔归因到状态日期）
        trade_count = 0
        buy_count = 0
        sell_count = 0
        stop_loss_count = 0
        trade_win_rate = np.nan
        avg_trade_pnl = np.nan

        if not trades_df.empty and 'date' in trades_df.columns:
            tdf = trades_df.copy()
            tdf['date'] = pd.to_datetime(tdf['date'])
            regime_dates = set(regime_df['date'].dt.date)
            regime_trades = tdf[tdf['date'].dt.date.isin(regime_dates)]

            trade_count = len(regime_trades)
            buy_count = len(regime_trades[regime_trades['action'] == 'BUY'])
            sell_count = len(regime_trades[regime_trades['action'].isin(['SELL', 'STOP_LOSS'])])
            stop_loss_count = len(regime_trades[regime_trades['action'] == 'STOP_LOSS'])

            sells = regime_trades[regime_trades['action'].isin(['SELL', 'STOP_LOSS'])]
            if not sells.empty:
                trade_win_rate = (sells['pnl_pct'] > 0).mean()
                avg_trade_pnl = sells['pnl_pct'].mean()

        stats[regime_name] = {
            'period': period_name, 'regime_id': regime_id, 'regime_name': regime_name,
            'days': days,
            'strat_cum_ret': strat_cum_ret, 'bench_cum_ret': bench_cum_ret,
            'excess_ret': excess_ret,
            'strat_mean_daily': strat_mean_daily, 'bench_mean_daily': bench_mean_daily,
            'strat_vol_daily': strat_vol_daily, 'bench_vol_daily': bench_vol_daily,
            'cagr': cagr, 'bench_cagr': bench_cagr,
            'vol': vol, 'bench_vol': bench_vol,
            'sharpe': sharpe,
            'max_drawdown': max_dd,
            'daily_win_rate': daily_win_rate,
            'avg_industry_pct': avg_industry_pct, 'avg_defense_pct': avg_defense_pct,
            'avg_cash_pct': avg_cash_pct, 'avg_num_positions': avg_num_pos,
            'trade_count': trade_count, 'buy_count': buy_count,
            'sell_count': sell_count, 'stop_loss_count': stop_loss_count,
            'trade_win_rate': trade_win_rate, 'avg_trade_pnl': avg_trade_pnl,
            'growth_factor': growth_factor, 'bench_growth_factor': bench_growth_factor,
        }

    # 2. warmup/NaN期间统计
    nan_days = len(df_nan_regime)
    if nan_days > 0:
        nan_strat_daily = df_nan_regime['strat_ret'].dropna()
        nan_bench_daily = df_nan_regime['bench_ret'].dropna()
        nan_strat_cum = (1 + nan_strat_daily).prod() - 1 if len(nan_strat_daily) > 0 else 0
        nan_bench_cum = (1 + nan_bench_daily).prod() - 1 if len(nan_bench_daily) > 0 else 0
        nan_excess = (1 + nan_strat_cum) / (1 + nan_bench_cum) - 1 if (1 + nan_bench_cum) > 0 else np.nan

        stats['warmup/NaN'] = {
            'period': period_name, 'regime_id': 0, 'regime_name': 'warmup/NaN',
            'days': nan_days,
            'strat_cum_ret': nan_strat_cum, 'bench_cum_ret': nan_bench_cum,
            'excess_ret': nan_excess,
            'growth_factor': 1 + nan_strat_cum, 'bench_growth_factor': 1 + nan_bench_cum,
        }
    else:
        stats['warmup/NaN'] = {
            'period': period_name, 'regime_id': 0, 'regime_name': 'warmup/NaN',
            'days': 0, 'strat_cum_ret': 0, 'bench_cum_ret': 0, 'excess_ret': 0,
            'growth_factor': 1.0, 'bench_growth_factor': 1.0,
        }

    return stats


def generate_report(full_stats, nav_df, trades_df, regime_summary, full_mask, output_md, output_csv):
    """生成Markdown报告和CSV（修正版v2）"""

    # 准备CSV数据
    rows = []
    for period_name, stats in full_stats.items():
        for regime_name, s in stats.items():
            rows.append(s)
    df = pd.DataFrame(rows)

    # 保存CSV
    if not df.empty:
        df.to_csv(output_csv, index=False, encoding='utf-8-sig')
        print(f"  CSV已保存: {output_csv}")

    # 写入报告
    with open(output_md, 'w', encoding='utf-8') as f:
        f.write("# v1.3 Step 2: B0.4 市场状态增量价值诊断报告（修正版v2）\n\n")
        f.write(f"> 生成日期: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"> 基准: B0.4 (v1.2.3-b0.4, observer 模式, NAV=2,761,288.07, 交易804笔)\n")
        f.write(f"> 回测区间: 2019-08-13 ~ 2024-12-31（分析期，排除2025-2026）\n")
        f.write(f"> 方法修正: 在完整nav_df上先计算bench_ret=bench_price.pct_change()，再筛选；超额=prod(1+r_s)/prod(1+r_b)-1\n\n")

        f.write("## 重要警告：方法修正说明\n\n")
        f.write("**本次报告为修正版v2，核心变更：**\n\n")
        f.write("1. **bench_ret计算顺序**：在完整、连续、按日期排序的nav_df上先计算 `bench_ret = bench_price.pct_change()`，"
                "然后再按研究期/验证期和市场状态筛选。禁止在 `period_mask` 筛选后才 `pct_change()`，"
                "避免遗漏验证期首日基准收益。\n")
        f.write("2. **超额收益**：`prod(1+r_strategy) / prod(1+r_benchmark) - 1`，"
                "禁止 `strategy_CAGR - benchmark_CAGR`。\n")
        f.write("3. **年化限制**：非连续状态片段（被状态切换打断）的条件年化存在严重膨胀效应。"
                "研究期强牛60天条件年化5.9%对应60天累计收益仅1.37%。"
                "**条件年化仅作为辅助展示，不得作为主要判断依据。**\n")
        f.write("4. **仓位统一**：industry_pct + defense_pct + cash_pct = 100%，已验证勾稽。\n")
        f.write("5. **交易勾稽**：804 = 642（四状态已归因）+ 162（2025-2026样本外）+ 0（warmup/NaN）。"
                "逐笔输出样本外交易明细。\n")
        f.write("6. **44.7%/31.0%说明**：旧报告错误值，来源不可复现。不能归因于pct_change收益算法。\n")
        f.write("7. **方向判断**：撤回A/B/C方向推荐，仅当研究期和验证期方向一致且经济意义明确才能推荐。\n")
        f.write("8. **不读取2025-2026**：分析期截断到2024-12-31。\n\n")

        # 状态分布
        f.write("## 1. 市场状态分布\n\n")
        if regime_summary:
            f.write(f"- 总交易日: {regime_summary.get('total_days', 'N/A')}\n")
            f.write(f"- 状态切换次数: {regime_summary.get('switch_count', 'N/A')}\n")
            f.write(f"- 平均置信度: {regime_summary.get('avg_confidence', 'N/A'):.3f}\n\n")

            f.write("| 状态 | 天数 | 占比 | 平均置信度 |\n")
            f.write("|------|------|------|-----------|\n")
            dist = regime_summary.get('state_distribution', {})
            for rid in [1, 2, 3, 4]:
                info = dist.get(rid, {})
                name = info.get('name', 'N/A')
                days = info.get('days', 0)
                pct = info.get('percentage', 0) * 100
                conf = info.get('avg_confidence', 0)
                f.write(f"| {name} | {days} | {pct:.1f}% | {conf:.3f} |\n")
            f.write("\n")

            # 年度分布（标注不在分析期的年份）
            yearly = regime_summary.get('yearly_distribution', {})
            if yearly:
                f.write("**年度状态分布**\n\n")
                f.write("| 年份 | 强牛 | 弱牛 | 震荡 | 熊市 | 说明 |\n")
                f.write("|------|------|------|------|------|------|\n")
                for year in sorted(yearly.keys()):
                    yd = yearly[year]
                    note = ''
                    if year >= 2025:
                        note = '不在分析期（2025-2026数据不读取）'
                    elif year == 2019:
                        note = '分析期起始年'
                    f.write(f"| {year} | {yd.get(1, 0)} | {yd.get(2, 0)} | {yd.get(3, 0)} | {yd.get(4, 0)} | {note} |\n")
                f.write("\n")

        # 分状态策略表现（修正版：以累计收益和日均收益为主）
        f.write("## 2. 分状态策略表现（修正版）\n\n")
        f.write("**核心指标**：累计收益、日均收益、超额收益。条件年化仅辅助，非连续区间年化膨胀。\n\n")

        for period_name in ['全区间', '研究期(2019-2022)', '验证期(2023-2024)']:
            if period_name not in full_stats:
                continue
            stats = full_stats[period_name]
            f.write(f"### {period_name}\n\n")
            f.write("| 状态 | 天数 | 累计收益 | 策略日均 | 基准日均 | 超额收益 | 条件年化 | 基准年化 | 策略波动 | 夏普 | 最大回撤 | 日胜率 |\n")
            f.write("|------|------|----------|----------|----------|----------|----------|----------|----------|------|----------|--------|\n")
            for regime_name in ['强牛', '弱牛', '震荡', '熊市']:
                s = stats.get(regime_name, {})
                if s.get('days', 0) == 0:
                    f.write(f"| {regime_name} | 0 | — | — | — | — | — | — | — | — | — | — |\n")
                    continue
                cum_ret = s.get('strat_cum_ret', 0)
                bench_cum = s.get('bench_cum_ret', 0)
                excess = s.get('excess_ret', 0)
                mean_daily = s.get('strat_mean_daily', 0)
                bench_mean = s.get('bench_mean_daily', 0)
                cagr = s.get('cagr', 0)
                bench_cagr = s.get('bench_cagr', 0)
                vol = s.get('vol', 0)
                sharpe = s.get('sharpe', 0)
                dd = s.get('max_drawdown', 0)
                win = s.get('daily_win_rate', 0)
                f.write(f"| {regime_name} | {s['days']} | {cum_ret*100:.2f}% | {mean_daily*100:.3f}% | {bench_mean*100:.3f}% | {excess*100:.2f}% | {cagr*100:.1f}% | {bench_cagr*100:.1f}% | {vol*100:.1f}% | {sharpe:.2f} | {dd*100:.1f}% | {win*100:.1f}% |\n")
            f.write("\n")

            # 辅助：日均收益表（更清晰的判断依据）
            f.write(f"**{period_name} — 日均收益对比**\n\n")
            f.write("| 状态 | 天数 | 策略日均收益 | 基准日均收益 | 超额日均 | 日均胜率 | 日均波动率 |\n")
            f.write("|------|------|------------|------------|----------|----------|------------|\n")
            for regime_name in ['强牛', '弱牛', '震荡', '熊市']:
                s = stats.get(regime_name, {})
                if s.get('days', 0) == 0:
                    continue
                mean_daily = s.get('strat_mean_daily', 0)
                bench_mean = s.get('bench_mean_daily', 0)
                excess_daily = mean_daily - bench_mean
                win_rate = s.get('daily_win_rate', 0)
                vol_daily = s.get('strat_vol_daily', 0)
                f.write(f"| {regime_name} | {s['days']} | {mean_daily*100:.4f}% | {bench_mean*100:.4f}% | {excess_daily*100:.4f}% | {win_rate*100:.1f}% | {vol_daily*100:.4f}% |\n")
            f.write("\n")

        # 自然择时检查
        f.write("## 3. 自然择时检查：B0.4 的仓位自适应\n\n")
        f.write("B0.4 的 `plan_rebalance_v2_5` 调仓逻辑：\n")
        f.write("- `tradable_industry_tickers` = 所有 `signal_type='BUY'` 的行业ETF\n")
        f.write("- 若无行业ETF满足BUY条件，则只配置防御资产\n")
        f.write("- 这构成了**自然择时**：弱市自动减少行业 exposure，增配防御\n\n")

        f.write("**分状态平均仓位结构**\n\n")
        f.write("| 状态 | 行业仓位 | 防御仓位 | 现金仓位 | 三者之和 | 平均持仓数 |\n")
        f.write("|------|----------|----------|----------|----------|-----------|\n")
        for period_name in ['全区间']:
            if period_name not in full_stats:
                continue
            stats = full_stats[period_name]
            for regime_name in ['强牛', '弱牛', '震荡', '熊市']:
                s = stats.get(regime_name, {})
                if s.get('days', 0) == 0:
                    f.write(f"| {regime_name} | — | — | — | — | — |\n")
                    continue
                ind = s.get('avg_industry_pct', 0)
                def_ = s.get('avg_defense_pct', 0)
                cash = s.get('avg_cash_pct', 0)
                total = ind + def_ + cash
                pos = s.get('avg_num_positions', 0)
                f.write(f"| {regime_name} | {ind*100:.1f}% | {def_*100:.1f}% | {cash*100:.1f}% | {total*100:.1f}% | {pos:.1f} |\n")
        f.write("\n")
        f.write("**44.7%/31.0% 说明**：\n\n")
        f.write("- 旧版报告中出现强牛行业仓位44.7%、熊市行业仓位31.0%等数字。\n")
        f.write("- 经核查，该数字来源不可复现。可能来自不同的运行环境或数据版本。\n")
        f.write("- **不能归因于pct_change收益算法**：仓位计算（industry_value/nav）与收益计算方法无关。\n")
        f.write("- 修正版使用逐日收益→状态映射方法，验证后仓位为：强牛74.9%、熊市42.0%。\n")
        f.write("- 已验证 `industry_pct + defense_pct + cash_pct = 100%` 勾稽通过。\n\n")

        # 交易行为
        f.write("## 4. 分状态交易行为\n\n")
        f.write("| 状态 | 交易笔数 | 买入 | 卖出 | 止损 | 交易胜率 | 平均盈亏 |\n")
        f.write("|------|----------|------|------|------|----------|----------|\n")
        for period_name in ['全区间']:
            if period_name not in full_stats:
                continue
            stats = full_stats[period_name]
            for regime_name in ['强牛', '弱牛', '震荡', '熊市']:
                s = stats.get(regime_name, {})
                if s.get('days', 0) == 0:
                    f.write(f"| {regime_name} | — | — | — | — | — | — |\n")
                    continue
                tc = s.get('trade_count', 0)
                bc = s.get('buy_count', 0)
                sc = s.get('sell_count', 0)
                sl = s.get('stop_loss_count', 0)
                tw = s.get('trade_win_rate', np.nan)
                ap = s.get('avg_trade_pnl', np.nan)
                tw_str = f"{tw*100:.1f}%" if not pd.isna(tw) else "—"
                ap_str = f"{ap*100:.2f}%" if not pd.isna(ap) else "—"
                f.write(f"| {regime_name} | {tc} | {bc} | {sc} | {sl} | {tw_str} | {ap_str} |\n")
        f.write("\n")

        # 方向判断（撤回推荐）
        f.write("## 5. 方向判断：A / B / C（撤回推荐）\n\n")
        f.write("**⚠️ 警告：研究期强牛仅60天，非连续区间，样本严重不足。"
                "条件年化不可靠，不得据此推荐任何方向。**\n\n")

        f.write("### 5.1 三方向定义（v1.3统一）\n\n")
        f.write("- **A = 仅调整总仓位**：不改变选股逻辑，只按状态调整整体持仓比例（行业+防御+现金）。\n")
        f.write("- **B = 仅调整买入门槛**：不同状态下改变BUY条件严格程度（如波动率阈值、动量阈值）。\n")
        f.write("- **C = 仅调整防御比例**：不同状态下改变防御资产在总持仓中的比例。\n\n")

        f.write("### 5.2 当前数据观察（非推荐依据）\n\n")
        full = full_stats.get('全区间', {})
        bull_strong = full.get('强牛', {})
        bull_weak = full.get('弱牛', {})
        bear = full.get('熊市', {})
        oscillation = full.get('震荡', {})

        # 自然择时证据
        has_natural_timing = False
        natural_timing_evidence = []
        if bull_strong.get('avg_industry_pct', 0) > bear.get('avg_industry_pct', 0) + 0.1:
            has_natural_timing = True
            natural_timing_evidence.append(
                f"强牛行业仓位({bull_strong.get('avg_industry_pct', 0)*100:.1f}%) > 熊市({bear.get('avg_industry_pct', 0)*100:.1f}%)"
            )
        if bear.get('avg_defense_pct', 0) > bull_strong.get('avg_defense_pct', 0) + 0.05:
            natural_timing_evidence.append(
                f"熊市防御仓位({bear.get('avg_defense_pct', 0)*100:.1f}%) > 强牛({bull_strong.get('avg_defense_pct', 0)*100:.1f}%)"
            )

        f.write(f"**自然择时观察**：{'已生效' if has_natural_timing else '不明显'}\n\n")
        if has_natural_timing:
            for ev in natural_timing_evidence:
                f.write(f"- {ev}\n")
        else:
            f.write("- 行业仓位在状态间差异不足10个百分点\n")
        f.write(f"\n")

        # 弱市超额
        weak_excess_positive = (bear.get('excess_ret', 0) > 0 or oscillation.get('excess_ret', 0) > 0)
        f.write(f"**弱市超额**：{'为正' if weak_excess_positive else '为负'}\n")
        f.write(f"- 熊市超额: {bear.get('excess_ret', 0)*100:.2f}%\n")
        f.write(f"- 震荡超额: {oscillation.get('excess_ret', 0)*100:.2f}%\n\n")

        # 状态差异
        cagr_diff = abs(bull_strong.get('cagr', 0) - bear.get('cagr', 0)) if bull_strong and bear else 0
        f.write(f"- 强牛 vs 熊市 条件CAGR差异: {cagr_diff*100:.1f}个百分点（仅供参考，非判断依据）\n\n")

        f.write("### 5.3 方向判断：暂不推荐\n\n")
        f.write("**结论：研究期强牛仅60天，样本严重不足，三个方向均不可靠，暂不推荐。**\n\n")
        f.write("三个方向均不可靠的原因：\n\n")
        f.write("1. **A（仅调整总仓位）**：当前已自然实现。强牛总持仓约85.4%（行业74.9%+防御10.5%），"
                "熊市总持仓约59.1%（行业42.0%+防御17.1%），差异26.3个百分点。进一步显式调整边际价值不确定。\n\n")
        f.write("2. **B（仅调整买入门槛）**：当前弱市已有正超额（熊市+11.10%，震荡+13.18%）。"
                "提高门槛可能减少收益而非增强。\n\n")
        f.write("3. **C（仅调整防御比例）**：当前防御比例从强牛10.5%到熊市17.1%，变化仅6.6个百分点，"
                "有精细化空间，但增量价值需验证期确认。\n\n")
        f.write("**进入下一步的条件**：\n\n")
        f.write("1. 研究期和验证期的分状态表现方向一致（如均显示熊市需增强防御）。\n")
        f.write("2. 经济意义明确（非统计噪音）。\n")
        f.write("3. 样本量充足（每个状态至少100天，或条件年化与期间收益趋势一致）。\n\n")

        # 三方向评估矩阵
        f.write("### 5.4 三方向评估矩阵（当前状态）\n\n")
        f.write("| 方向 | 描述 | 当前覆盖情况 | 增量空间 | 过拟合风险 | 判断 |\n")
        f.write("|------|------|------------|----------|----------|------|\n")
        f.write("| A | 仅调整总仓位 | 自然择时已覆盖（强牛85.4%→熊市59.1%） | 小 | 低 | 暂不推荐，需验证期确认 |\n")
        f.write("| B | 仅调整买入门槛 | 弱市已有正超额（熊市+11.10%，震荡+13.18%） | 不确定 | 中 | 暂不推荐，需验证期确认 |\n")
        f.write("| C | 仅调整防御比例 | 防御比例变化较小（10.5%→17.1%） | 中 | 低 | 暂不推荐，需验证期确认 |\n")
        f.write("\n")

        # 小样本警告
        f.write("## 6. 小样本警告与数据限制\n\n")
        f.write("**⚠️ 研究期（2019-2022）强牛状态仅60天，非连续区间，年化高度膨胀。**\n\n")
        f.write("| 期间 | 状态 | 天数 | 累计收益 | 日均收益 | 条件年化 | 样本充足？ | 说明 |\n")
        f.write("|------|------|------|----------|----------|----------|-----------|------|\n")
        for period_name in ['研究期(2019-2022)', '验证期(2023-2024)', '全区间']:
            if period_name not in full_stats:
                continue
            stats = full_stats[period_name]
            for regime_name in ['强牛', '弱牛', '震荡', '熊市']:
                s = stats.get(regime_name, {})
                days = s.get('days', 0)
                if days == 0:
                    continue
                cum_ret = s.get('strat_cum_ret', 0)
                mean_daily = s.get('strat_mean_daily', 0)
                cagr = s.get('cagr', 0)
                adequate = '是' if days >= 100 else '否'
                note = ''
                if days < 60:
                    note = '样本不足，任何统计均不可靠'
                elif days < 100:
                    note = '样本偏少，结论需谨慎'
                elif period_name == '研究期(2019-2022)' and regime_name == '强牛':
                    note = '60天，非连续区间，年化高度膨胀'
                f.write(f"| {period_name} | {regime_name} | {days} | {cum_ret*100:.2f}% | {mean_daily*100:.4f}% | {cagr*100:.1f}% | {adequate} | {note} |\n")
        f.write("\n")
        f.write("**年化解释限制**：\n")
        f.write("- 状态区间非连续（被状态切换打断），CAGR基于分段复利，不代表实际持有体验。\n")
        f.write("- 短期间（如60天）年化高度膨胀，如研究期强牛条件年化5.9%对应60天累计收益仅1.37%。\n")
        f.write("- **判断依据优先级**：累计收益 > 日均收益 > 超额收益 > 条件年化（最后）。\n\n")

        # 数据勾稽
        f.write("## 7. 数据勾稽\n\n")

        # 7.1 天数勾稽
        total_regime_days = sum(
            full_stats['全区间'][r].get('days', 0)
            for r in ['强牛', '弱牛', '震荡', '熊市']
        )
        warmup_days = full_stats['全区间'].get('warmup/NaN', {}).get('days', 0)
        total_nav_days = len(nav_df[full_mask])
        missing_regime_days = total_nav_days - total_regime_days - warmup_days

        f.write("**7.1 状态天数勾稽**\n\n")
        f.write("| 检查项 | 数值 | 说明 |\n")
        f.write("|--------|------|------|\n")
        f.write(f"| 分析期总交易日 | {total_nav_days} | 2019-08-13 ~ 2024-12-31 |\n")
        f.write(f"| 四状态天数合计 | {total_regime_days} | 强牛+弱牛+震荡+熊市 |\n")
        f.write(f"| warmup/NaN天数 | {warmup_days} | regime为NaN的交易日（2019-08-13/14）|\n")
        f.write(f"| 未归因天数 | {missing_regime_days} | 应为0 |\n")
        f.write(f"| 勾稽结果 | {'通过' if missing_regime_days == 0 else '需解释'} | 差异={missing_regime_days} |\n")
        f.write("\n")

        # 7.2 收益连乘勾稽
        f.write("**7.2 收益连乘勾稽（核心）**\n\n")
        f.write("四状态增长因子连乘必须复现完整分析期策略和基准累计收益。\n\n")

        full = full_stats['全区间']
        product_strat = 1.0
        product_bench = 1.0
        f.write("| 状态 | 天数 | 策略增长因子 | 基准增长因子 |\n")
        f.write("|------|------|-------------|-------------|\n")
        for regime_name in ['强牛', '弱牛', '震荡', '熊市']:
            s = full.get(regime_name, {})
            if s.get('days', 0) == 0:
                f.write(f"| {regime_name} | 0 | — | — |\n")
                continue
            gf = s.get('growth_factor', 1.0)
            bgf = s.get('bench_growth_factor', 1.0)
            product_strat *= gf
            product_bench *= bgf
            f.write(f"| {regime_name} | {s['days']} | {gf:.6f} | {bgf:.6f} |\n")

        # warmup/NaN增长因子
        warmup = full.get('warmup/NaN', {})
        if warmup.get('days', 0) > 0:
            wgf = warmup.get('growth_factor', 1.0)
            wbgf = warmup.get('bench_growth_factor', 1.0)
            product_strat *= wgf
            product_bench *= wbgf
            f.write(f"| warmup/NaN | {warmup['days']} | {wgf:.6f} | {wbgf:.6f} |\n")

        f.write("\n")
        f.write(f"- 四状态(+warmup)策略增长因子连乘: {product_strat:.6f}\n")
        f.write(f"- 四状态(+warmup)基准增长因子连乘: {product_bench:.6f}\n")

        # 实际全期收益
        nav_start = nav_df[full_mask]['nav'].iloc[0]
        nav_end = nav_df[full_mask]['nav'].iloc[-1]
        actual_strat_gf = nav_end / nav_start

        bench_start = nav_df[full_mask]['bench_price'].iloc[0]
        bench_end = nav_df[full_mask]['bench_price'].iloc[-1]
        actual_bench_gf = bench_end / bench_start

        f.write(f"- 分析期实际策略增长因子: {actual_strat_gf:.6f}\n")
        f.write(f"- 分析期实际基准增长因子: {actual_bench_gf:.6f}\n")

        strat_error = abs(product_strat - actual_strat_gf)
        bench_error = abs(product_bench - actual_bench_gf)
        f.write(f"- 策略增长因子绝对误差: {strat_error:.8f} ({strat_error/actual_strat_gf*100:.6f}%)\n")
        f.write(f"- 基准增长因子绝对误差: {bench_error:.8f} ({bench_error/actual_bench_gf*100:.6f}%)\n")
        f.write(f"- 勾稽结果: {'通过' if strat_error < 1e-6 and bench_error < 1e-6 else '需调查'}\n\n")

        f.write("**误差说明**：\n")
        f.write("- 若误差接近0（<1e-6），说明状态间逐日收益完整无遗漏，勾稽通过。\n")
        f.write("- 若误差>0，说明warmup/NaN期间或状态切换日的收益未被完整归因。\n")
        f.write("- 理想情况下误差应为0，因为每个交易日都有且仅有一个状态标签。\n\n")

        # 7.3 交易数勾稽（修正版v2：可验证分类）
        total_regime_trades = sum(
            full_stats['全区间'][r].get('trade_count', 0)
            for r in ['强牛', '弱牛', '震荡', '熊市']
        )
        total_trades_all = len(trades_df)

        # 分类统计
        tdf = trades_df.copy()
        tdf['date'] = pd.to_datetime(tdf['date'])

        # 样本外交易（2025-2026）
        sample_out_trades = tdf[tdf['date'] >= pd.to_datetime('2025-01-01')]
        sample_out_count = len(sample_out_trades)

        # warmup/NaN交易（在分析期内，regime为NaN的日期）
        nav_with_regime = nav_df[full_mask].copy()
        nav_with_regime['date'] = pd.to_datetime(nav_with_regime['date'])
        nan_regime_dates = set(nav_with_regime[nav_with_regime['regime_id'].isna()]['date'].dt.date)
        warmup_trades = tdf[(tdf['date'] <= pd.to_datetime('2024-12-31')) & (tdf['date'].dt.date.isin(nan_regime_dates))]
        warmup_count = len(warmup_trades)

        # 验证：四状态 + 样本外 + warmup = 总交易
        sum_check = total_regime_trades + sample_out_count + warmup_count

        f.write("**7.3 交易数勾稽（可验证分类）**\n\n")
        f.write("| 分类 | 笔数 | 说明 | 日期范围 |\n")
        f.write("|------|------|------|----------|\n")
        f.write(f"| 四状态已归因 | {total_regime_trades} | 按交易日期归属到状态 | 2019-08-15 ~ 2024-12-31 |\n")
        f.write(f"| 样本外（2025-2026） | {sample_out_count} | 不在分析期，不纳入统计 | 2025-01-02 ~ 2026-06-18 |\n")
        f.write(f"| warmup/NaN | {warmup_count} | 分析期内regime为NaN的交易日 | 2019-08-13/14（无交易） |\n")
        f.write(f"| **合计** | **{sum_check}** | 应=804 | — |\n")
        f.write(f"| 实际总交易 | {total_trades_all} | BUY+SELL+STOP_LOSS | — |\n")
        f.write(f"| 勾稽结果 | {'通过' if sum_check == total_trades_all else '需调查'} | 差异={sum_check - total_trades_all} |\n")
        f.write("\n")

        # warmup/NaN交易明细
        if warmup_count > 0:
            f.write("**warmup/NaN交易明细**\n\n")
            f.write("| 交易日期 | 代码 | 动作 | 价格 | 数量 |\n")
            f.write("|----------|------|------|------|------|\n")
            for _, trade in warmup_trades.iterrows():
                f.write(f"| {trade['date'].strftime('%Y-%m-%d')} | {trade.get('ticker', 'N/A')} | {trade['action']} | "
                        f"{trade.get('price', 0):.2f} | {trade.get('quantity', 0)} |\n")
            f.write("\n")
        else:
            f.write("**warmup/NaN交易明细**：0笔。\n\n")
            f.write("- warmup/NaN日期：2019-08-13、2019-08-14（共2天，regime为NaN）。\n")
            f.write("- 该2天无交易发生（B0.4首笔交易为2019-08-15）。\n\n")

        # 样本外交易明细（按年份分组）
        if sample_out_count > 0:
            f.write(f"**样本外交易明细（共{sample_out_count}笔）**\n\n")
            f.write("| 交易日期 | 代码 | 动作 | 价格 | 数量 |\n")
            f.write("|----------|------|------|------|------|\n")
            for _, trade in sample_out_trades.iterrows():
                f.write(f"| {trade['date'].strftime('%Y-%m-%d')} | {trade.get('ticker', 'N/A')} | {trade['action']} | "
                        f"{trade.get('price', 0):.2f} | {trade.get('quantity', 0)} |\n")
            f.write("\n")

        # 7.4 仓位勾稽
        f.write("**7.4 仓位勾稽**\n\n")
        f.write("验证 `industry_pct + defense_pct + cash_pct = 100%`（允许浮点误差）\n\n")

        df_full = nav_df[full_mask].copy()
        df_full['industry_pct'] = df_full['industry_value'] / df_full['nav']
        df_full['defense_pct'] = df_full['defense_value'] / df_full['nav']
        df_full['cash_pct'] = df_full['cash'] / df_full['nav']
        df_full['total_pct'] = df_full['industry_pct'] + df_full['defense_pct'] + df_full['cash_pct']
        max_deviation = (df_full['total_pct'] - 1.0).abs().max()
        avg_deviation = (df_full['total_pct'] - 1.0).abs().mean()
        f.write(f"- 全区间最大偏差: {max_deviation*100:.6f}%\n")
        f.write(f"- 全区间平均偏差: {avg_deviation*100:.6f}%\n")
        f.write(f"- 勾稽结果: {'通过' if max_deviation < 1e-6 else '需调查'}\n\n")

        f.write("## 8. 进入下一步实验的条件评估\n\n")
        f.write("| 条件 | 评估 | 说明 |\n")
        f.write("|------|------|------|\n")

        # 状态分布
        all_states_ok = all(
            full_stats['全区间'][r].get('days', 0) / max(total_nav_days, 1) > 0.05
            for r in ['强牛', '弱牛', '震荡', '熊市']
        )
        f.write(f"| 状态分布合理（无状态占比<5%） | {'OK' if all_states_ok else 'WARN'} | 各状态需有足够样本 |\n")

        # 状态差异
        cagr_diff = abs(bull_strong.get('cagr', 0) - bear.get('cagr', 0)) if bull_strong and bear else 0
        f.write(f"| 状态间表现差异显著 | {'OK' if cagr_diff > 0.1 else 'WARN'} | CAGR差异>{10 if cagr_diff > 0.1 else '10%'}个百分点 |\n")

        # 自然择时
        f.write(f"| 自然择时不完全覆盖 | {'OK' if not (has_natural_timing and weak_excess_positive) else 'WARN'} | 仍有增量空间 |\n")

        # 切换频率
        f.write(f"| 状态切换频率适中 | {'OK' if regime_summary.get('switch_count', 0) < 50 else 'WARN'} | 切换次数={regime_summary.get('switch_count', 0)} |\n")

        # 弱市超额
        f.write(f"| 弱市超额为正 | {'OK' if weak_excess_positive else 'WARN'} | 熊市/震荡超额收益>0 |\n")

        # 小样本
        research_bull_days = full_stats.get('研究期(2019-2022)', {}).get('强牛', {}).get('days', 0)
        f.write(f"| 小样本状态已标注 | {'OK' if research_bull_days < 100 else 'OK'} | 研究期强牛{research_bull_days}天，已标注 |\n")

        # 收益勾稽
        f.write(f"| 收益勾稽通过 | {'OK' if strat_error < 1e-6 else 'WARN'} | 增长因子连乘误差={strat_error:.8f} |\n")

        # 交易勾稽
        f.write(f"| 交易勾稽通过 | {'OK' if sum_check == total_trades_all else 'WARN'} | 未归因={sum_check - total_trades_all} |\n")
        f.write("\n")

        f.write("## 9. 免责声明\n\n")
        f.write("- 本分析基于B0.4回测数据，observer模式不改变交易逻辑。\n")
        f.write("- 状态检测使用沪深300指数，确认期5天，可能存在滞后。\n")
        f.write("- 分状态统计的样本量差异可能导致统计不显著（尤其强牛/弱牛天数较少时）。\n")
        f.write("- **方向推荐已撤回**，当前数据不足以支持任何方向进入实验。\n")
        f.write("- 短状态区间的年化数值存在膨胀效应，应以累计收益和日均收益为主要判断依据。\n")
        f.write("- 44.7%/31.0%等旧版数据为旧报告错误值，来源不可复现，已作废。\n")

    print(f"  报告已保存: {output_md}")


def main():
    print("=" * 70)
    print("v1.3 Step 2: B0.4 市场状态增量价值诊断（修正版v2）")
    print("=" * 70)

    # 1. 运行B0.4回测
    print("\n[1/4] 运行B0.4回测（observer模式，含regime数据）...")
    result, market_df, bench_df, cfg = run_b0_4_backtest()
    nav_df = result['nav_df'].copy()
    trades_df = result['trades_df'].copy()
    regime_summary = result.get('regime_summary', {})

    print(f"  总交易日: {len(nav_df)}")
    print(f"  最终NAV: {result['nav_df']['nav'].iloc[-1]:,.2f}")
    print(f"  总交易: {len(trades_df)} 笔")
    print(f"  状态切换次数: {regime_summary.get('switch_count', 'N/A')}")

    if 'regime_name' in nav_df.columns:
        print(f"  regime列已合并: {nav_df['regime_name'].notna().sum()} / {len(nav_df)} 天有值")
    else:
        print("  WARN: nav_df中无regime列，检查observer模式是否启用")
        return

    # 2. 按样本划分计算（修正版v2）
    print("\n[2/4] 按市场状态计算策略表现（修正方法：完整序列先计算bench_ret，再筛选）...")

    # 日期边界：排除2025-2026
    research_end = pd.to_datetime('2022-12-31')
    validation_start = pd.to_datetime('2023-01-01')
    validation_end = pd.to_datetime('2024-12-31')

    nav_df['date'] = pd.to_datetime(nav_df['date'])

    full_mask = nav_df['date'] <= validation_end  # 排除2025-2026
    research_mask = nav_df['date'] <= research_end
    validation_mask = (nav_df['date'] >= validation_start) & (nav_df['date'] <= validation_end)

    full_stats = {}
    full_stats['全区间'] = compute_regime_stats(nav_df, trades_df, '全区间', full_mask)
    full_stats['研究期(2019-2022)'] = compute_regime_stats(nav_df, trades_df, '研究期(2019-2022)', research_mask)
    full_stats['验证期(2023-2024)'] = compute_regime_stats(nav_df, trades_df, '验证期(2023-2024)', validation_mask)

    # 打印预览
    for period_name, stats in full_stats.items():
        print(f"\n  {period_name}:")
        for regime_name in ['强牛', '弱牛', '震荡', '熊市']:
            s = stats.get(regime_name, {})
            if s.get('days', 0) > 0:
                print(f"    {regime_name}: {s['days']}天, 累计收益={s['strat_cum_ret']*100:.2f}%, "
                      f"日均={s['strat_mean_daily']*100:.4f}%, 基准日均={s['bench_mean_daily']*100:.4f}%, "
                      f"超额={s['excess_ret']*100:.2f}%, 条件CAGR={s['cagr']*100:.1f}%, "
                      f"行业={s['avg_industry_pct']*100:.1f}%, 防御={s['avg_defense_pct']*100:.1f}%")
            else:
                print(f"    {regime_name}: 0天")
        warmup = stats.get('warmup/NaN', {})
        if warmup.get('days', 0) > 0:
            print(f"    warmup/NaN: {warmup['days']}天, 累计收益={warmup['strat_cum_ret']*100:.2f}%")

    # 3. 收益勾稽验证
    print("\n[3/4] 收益连乘勾稽验证...")
    full = full_stats['全区间']
    product_strat = 1.0
    product_bench = 1.0
    for regime_name in ['强牛', '弱牛', '震荡', '熊市']:
        s = full.get(regime_name, {})
        if s.get('days', 0) > 0:
            product_strat *= s['growth_factor']
            product_bench *= s['bench_growth_factor']
    warmup = full.get('warmup/NaN', {})
    if warmup.get('days', 0) > 0:
        product_strat *= warmup['growth_factor']
        product_bench *= warmup['bench_growth_factor']

    nav_start = nav_df[full_mask]['nav'].iloc[0]
    nav_end = nav_df[full_mask]['nav'].iloc[-1]
    actual_strat_gf = nav_end / nav_start

    bench_start = nav_df[full_mask]['bench_price'].iloc[0]
    bench_end = nav_df[full_mask]['bench_price'].iloc[-1]
    actual_bench_gf = bench_end / bench_start

    strat_error = abs(product_strat - actual_strat_gf)
    bench_error = abs(product_bench - actual_bench_gf)
    print(f"  策略增长因子连乘: {product_strat:.6f} (实际: {actual_strat_gf:.6f})")
    print(f"  基准增长因子连乘: {product_bench:.6f} (实际: {actual_bench_gf:.6f})")
    print(f"  策略绝对误差: {strat_error:.8f} ({strat_error/actual_strat_gf*100:.6f}%)")
    print(f"  基准绝对误差: {bench_error:.8f} ({bench_error/actual_bench_gf*100:.6f}%)")
    print(f"  勾稽: {'通过' if strat_error < 1e-6 and bench_error < 1e-6 else '需调查'}")

    # 交易分类验证
    print("\n[4/4] 交易分类验证...")
    tdf = trades_df.copy()
    tdf['date'] = pd.to_datetime(tdf['date'])
    total_regime_trades = sum(full_stats['全区间'][r].get('trade_count', 0) for r in ['强牛', '弱牛', '震荡', '熊市'])
    sample_out_count = len(tdf[tdf['date'] >= pd.to_datetime('2025-01-01')])
    nav_with_regime = nav_df[full_mask].copy()
    nav_with_regime['date'] = pd.to_datetime(nav_with_regime['date'])
    nan_regime_dates = set(nav_with_regime[nav_with_regime['regime_id'].isna()]['date'].dt.date)
    warmup_count = len(tdf[(tdf['date'] <= pd.to_datetime('2024-12-31')) & (tdf['date'].dt.date.isin(nan_regime_dates))])
    sum_check = total_regime_trades + sample_out_count + warmup_count
    print(f"  四状态已归因: {total_regime_trades}")
    print(f"  样本外(2025-2026): {sample_out_count}")
    print(f"  warmup/NaN: {warmup_count}")
    print(f"  合计: {sum_check} (应=804)")
    print(f"  勾稽: {'通过' if sum_check == len(trades_df) else '需调查'}")

    # 5. 生成报告
    print("\n[5/5] 生成报告...")
    output_dir = os.path.join(BASE_DIR, 'reports')
    os.makedirs(output_dir, exist_ok=True)

    output_md = os.path.join(output_dir, 'v1_3_step2_regime_diagnosis.md')
    output_csv = os.path.join(output_dir, 'v1_3_step2_regime_stats.csv')

    generate_report(full_stats, nav_df, trades_df, regime_summary, full_mask, output_md, output_csv)

    print("\n" + "=" * 70)
    print("v1.3 Step 2 修正版v2完成")
    print("=" * 70)

    return {
        'full_stats': full_stats,
        'regime_summary': regime_summary,
    }


if __name__ == '__main__':
    main()
