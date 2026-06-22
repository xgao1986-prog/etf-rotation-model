#!/usr/bin/env python3
"""
v1.3 Step 2: B0.4 市场状态增量价值诊断

目标：判断 B0.4 已有的自然择时是否足够，市场状态检测是否还有增量价值。

分析维度：
1. 状态分布：各状态覆盖天数、切换频率
2. 状态×策略表现：分状态计算策略收益、波动、夏普、回撤
3. 状态×基准表现：分状态计算基准收益（对比）
4. 自然择时检查：B0.4 在不同状态下的行业/防御/现金仓位分布
5. 交易行为：分状态统计交易频率、胜率
6. 方向判断：A(状态参数映射) / B(状态敏感仓位) / C(状态信号过滤) 哪个最值得实验

基准：v1.2.3 / B0.4（0bp，observer 模式已启用）
不修改生产策略、参数或冻结基线。
"""

import sys, os
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, 'src'))

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from collections import defaultdict

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

    # D盘02240cf backtest.py已内置observer模式，result['nav_df']已包含regime列
    # 无需手动merge，直接使用引擎返回的regime数据
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
    按市场状态计算策略表现统计。

    Args:
        nav_df: 回测nav_df（已合并regime列）
        trades_df: 交易记录
        period_name: 期间名称
        period_mask: nav_df上的布尔掩码

    Returns:
        dict: 各状态统计
    """
    df = nav_df[period_mask].copy()
    if df.empty:
        return {}

    # 确保regime列存在
    if 'regime_name' not in df.columns:
        return {}

    # 填充前向（regime只在确认切换日有值，中间日可能NaN）
    # 实际上 detect_history 返回的是每日状态，merge后应该每天都有
    # 但 warmup 期可能有NaN，用ffill填充
    df['regime_name'] = df['regime_name'].ffill()
    df['regime_id'] = df['regime_id'].ffill()

    stats = {}

    for regime_id, regime_name in [(1, '强牛'), (2, '弱牛'), (3, '震荡'), (4, '熊市')]:
        regime_df = df[df['regime_id'] == regime_id].copy()
        if regime_df.empty:
            stats[regime_name] = {
                'period': period_name,
                'regime_id': regime_id,
                'regime_name': regime_name,
                'days': 0,
            }
            continue

        regime_df = regime_df.sort_values('date')
        days = len(regime_df)

        # 策略收益（期间内）
        start_nav = regime_df['nav'].iloc[0]
        end_nav = regime_df['nav'].iloc[-1]
        period_return = (end_nav / start_nav) - 1
        years = days / 252
        cagr = (1 + period_return) ** (1 / years) - 1 if years > 0 and period_return > -1 else 0

        # 策略波动
        daily_rets = regime_df['nav'].pct_change().dropna()
        vol = daily_rets.std() * np.sqrt(252) if len(daily_rets) > 1 else 0
        sharpe = cagr / vol if vol > 0 else 0

        # 基准收益
        bench_start = regime_df['bench_price'].iloc[0]
        bench_end = regime_df['bench_price'].iloc[-1]
        bench_return = (bench_end / bench_start) - 1 if bench_start > 0 else 0
        bench_cagr = (1 + bench_return) ** (1 / years) - 1 if years > 0 and bench_return > -1 else 0

        # 基准波动
        bench_daily = regime_df['bench_price'].pct_change().dropna()
        bench_vol = bench_daily.std() * np.sqrt(252) if len(bench_daily) > 1 else 0

        # 超额收益
        excess_cagr = cagr - bench_cagr

        # 最大回撤（仅在当前状态内）
        peak = regime_df['nav'].cummax()
        dd = (regime_df['nav'] - peak) / peak
        max_dd = dd.min()

        # 胜率（日度）
        daily_win_rate = (daily_rets > 0).mean() if len(daily_rets) > 0 else 0

        # 自然择时：行业/防御/现金仓位
        avg_industry_pct = (regime_df['industry_value'] / regime_df['nav']).mean() if 'industry_value' in regime_df.columns else np.nan
        avg_defense_pct = (regime_df['defense_value'] / regime_df['nav']).mean() if 'defense_value' in regime_df.columns else np.nan
        avg_cash_pct = (regime_df['cash'] / regime_df['nav']).mean() if 'cash' in regime_df.columns else np.nan
        avg_num_pos = regime_df['num_positions'].mean() if 'num_positions' in regime_df.columns else np.nan

        # 交易统计（发生在该状态期间的交易）
        if not trades_df.empty and 'date' in trades_df.columns:
            tdf = trades_df.copy()
            tdf['date'] = pd.to_datetime(tdf['date'])
            regime_dates = set(regime_df['date'].dt.date)
            regime_trades = tdf[tdf['date'].dt.date.isin(regime_dates)]

            trade_count = len(regime_trades)
            buy_count = len(regime_trades[regime_trades['action'] == 'BUY'])
            sell_count = len(regime_trades[regime_trades['action'].isin(['SELL', 'STOP_LOSS'])])
            stop_loss_count = len(regime_trades[regime_trades['action'] == 'STOP_LOSS'])

            # 交易胜率
            sells = regime_trades[regime_trades['action'].isin(['SELL', 'STOP_LOSS'])]
            if not sells.empty:
                trade_win_rate = (sells['pnl_pct'] > 0).mean()
                avg_trade_pnl = sells['pnl_pct'].mean()
            else:
                trade_win_rate = np.nan
                avg_trade_pnl = np.nan
        else:
            trade_count = 0
            buy_count = 0
            sell_count = 0
            stop_loss_count = 0
            trade_win_rate = np.nan
            avg_trade_pnl = np.nan

        stats[regime_name] = {
            'period': period_name,
            'regime_id': regime_id,
            'regime_name': regime_name,
            'days': days,
            'period_return': period_return,
            'cagr': cagr,
            'volatility': vol,
            'sharpe': sharpe,
            'max_drawdown': max_dd,
            'daily_win_rate': daily_win_rate,
            'bench_return': bench_return,
            'bench_cagr': bench_cagr,
            'bench_volatility': bench_vol,
            'excess_cagr': excess_cagr,
            'avg_industry_pct': avg_industry_pct,
            'avg_defense_pct': avg_defense_pct,
            'avg_cash_pct': avg_cash_pct,
            'avg_num_positions': avg_num_pos,
            'trade_count': trade_count,
            'buy_count': buy_count,
            'sell_count': sell_count,
            'stop_loss_count': stop_loss_count,
            'trade_win_rate': trade_win_rate,
            'avg_trade_pnl': avg_trade_pnl,
        }

    return stats


def generate_report(full_stats, nav_df, trades_df, regime_summary, output_md, output_csv):
    """生成Markdown报告和CSV"""

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
        f.write("# v1.3 Step 2: B0.4 市场状态增量价值诊断报告\n\n")
        f.write(f"> 生成日期: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"> 基准: B0.4 (v1.2.3-b0.4, observer 模式)\n")
        f.write(f"> 回测区间: 2019-08-13 ~ {AS_OF_DATE}\n\n")

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

            # 年度分布
            yearly = regime_summary.get('yearly_distribution', {})
            if yearly:
                f.write("**年度状态分布**\n\n")
                f.write("| 年份 | 强牛 | 弱牛 | 震荡 | 熊市 |\n")
                f.write("|------|------|------|------|------|\n")
                for year in sorted(yearly.keys()):
                    yd = yearly[year]
                    f.write(f"| {year} | {yd.get(1, 0)} | {yd.get(2, 0)} | {yd.get(3, 0)} | {yd.get(4, 0)} |\n")
                f.write("\n")

        # 分状态策略表现
        f.write("## 2. 分状态策略表现\n\n")
        for period_name in ['全区间', '研究期(2019-2022)', '验证期(2023-2024)']:
            if period_name not in full_stats:
                continue
            stats = full_stats[period_name]
            f.write(f"### {period_name}\n\n")
            f.write("| 状态 | 天数 | 期间收益 | 策略年化 | 基准年化 | 超额年化 | 策略波动 | 夏普 | 最大回撤 | 日胜率 |\n")
            f.write("|------|------|----------|----------|----------|----------|----------|------|----------|--------|\n")
            for regime_name in ['强牛', '弱牛', '震荡', '熊市']:
                s = stats.get(regime_name, {})
                if s.get('days', 0) == 0:
                    f.write(f"| {regime_name} | 0 | — | — | — | — | — | — | — | — |\n")
                    continue
                period_ret = s.get('period_return', 0)
                cagr = s.get('cagr', 0)
                bench_cagr = s.get('bench_cagr', 0)
                excess = s.get('excess_cagr', 0)
                vol = s.get('volatility', 0)
                sharpe = s.get('sharpe', 0)
                dd = s.get('max_drawdown', 0)
                win = s.get('daily_win_rate', 0)
                f.write(f"| {regime_name} | {s['days']} | {period_ret*100:.1f}% | {cagr*100:.1f}% | {bench_cagr*100:.1f}% | {excess*100:.1f}% | {vol*100:.1f}% | {sharpe:.2f} | {dd*100:.1f}% | {win*100:.1f}% |\n")
            f.write("\n")

        # 自然择时检查
        f.write("## 3. 自然择时检查：B0.4 的仓位自适应\n\n")
        f.write("B0.4 的 `plan_rebalance_v2_5` 调仓逻辑：\n")
        f.write("- `tradable_industry_tickers` = 所有 `signal_type='BUY'` 的行业ETF\n")
        f.write("- 若无行业ETF满足BUY条件，则只配置防御资产\n")
        f.write("- 这构成了**自然择时**：弱市自动减少行业 exposure，增配防御\n\n")

        f.write("**分状态平均仓位结构**\n\n")
        f.write("| 状态 | 行业仓位 | 防御仓位 | 现金仓位 | 平均持仓数 |\n")
        f.write("|------|----------|----------|----------|-----------|\n")
        for period_name in ['全区间']:
            if period_name not in full_stats:
                continue
            stats = full_stats[period_name]
            for regime_name in ['强牛', '弱牛', '震荡', '熊市']:
                s = stats.get(regime_name, {})
                if s.get('days', 0) == 0:
                    f.write(f"| {regime_name} | — | — | — | — |\n")
                    continue
                ind = s.get('avg_industry_pct', 0)
                def_ = s.get('avg_defense_pct', 0)
                cash = s.get('avg_cash_pct', 0)
                pos = s.get('avg_num_positions', 0)
                f.write(f"| {regime_name} | {ind*100:.1f}% | {def_*100:.1f}% | {cash*100:.1f}% | {pos:.1f} |\n")
        f.write("\n")

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

        # 方向判断
        f.write("## 5. 方向判断：A / B / C 哪个最值得实验？\n\n")

        # 基于数据做判断
        full = full_stats.get('全区间', {})
        bull_strong = full.get('强牛', {})
        bull_weak = full.get('弱牛', {})
        bear = full.get('熊市', {})
        oscillation = full.get('震荡', {})

        # 判断逻辑
        has_natural_timing = False
        natural_timing_evidence = []

        # 检查行业仓位是否随状态变化
        if bull_strong.get('avg_industry_pct', 0) > bear.get('avg_industry_pct', 0) + 0.1:
            has_natural_timing = True
            natural_timing_evidence.append(
                f"强牛行业仓位({bull_strong.get('avg_industry_pct', 0)*100:.1f}%) > 熊市({bear.get('avg_industry_pct', 0)*100:.1f}%)，"
                f"差异{bull_strong.get('avg_industry_pct', 0)*100 - bear.get('avg_industry_pct', 0)*100:.1f}个百分点"
            )

        if bear.get('avg_defense_pct', 0) > bull_strong.get('avg_defense_pct', 0) + 0.05:
            natural_timing_evidence.append(
                f"熊市防御仓位({bear.get('avg_defense_pct', 0)*100:.1f}%) > 强牛({bull_strong.get('avg_defense_pct', 0)*100:.1f}%)"
            )

        # 检查超额收益在弱市是否为正（说明自然择时有效）
        weak_excess_positive = (bear.get('excess_cagr', 0) > 0 or oscillation.get('excess_cagr', 0) > 0)

        # 检查状态间表现差异是否大（如果差异大，说明参数映射有价值）
        cagr_diff = abs(bull_strong.get('cagr', 0) - bear.get('cagr', 0)) if bull_strong and bear else 0
        sharpe_diff = abs(bull_strong.get('sharpe', 0) - bear.get('sharpe', 0)) if bull_strong and bear else 0

        # 判断哪个方向最值得
        f.write("### 5.1 数据洞察\n\n")

        if has_natural_timing:
            f.write(f"**自然择时已生效**\n\n")
            for ev in natural_timing_evidence:
                f.write(f"- {ev}\n")
            f.write(f"\n")
        else:
            f.write(f"**自然择时不明显**\n\n")
            f.write(f"- 行业仓位在状态间差异不足10个百分点，信号过滤机制可能不够敏感\n\n")

        if weak_excess_positive:
            f.write(f"**弱市超额为正**：熊市/震荡期策略超额CAGR为正，说明现有防御规则已提供有效保护\n\n")
        else:
            f.write(f"**弱市超额为负**：熊市/震荡期策略跑输基准，防御规则保护不足\n\n")

        f.write(f"- 强牛 vs 熊市 CAGR差异: {cagr_diff*100:.1f}个百分点\n")
        f.write(f"- 强牛 vs 熊市 夏普差异: {sharpe_diff:.2f}\n\n")

        # 方向推荐
        f.write("### 5.2 方向推荐\n\n")

        # A: 状态参数映射 — 需要状态间差异大且稳定
        # B: 状态敏感仓位 — 如果自然择时已存在但不够精细
        # C: 状态信号过滤 — 如果自然择时不足，需要显式过滤

        # 核心判断：自然择时是否有效 + 是否足够精细
        # 如果自然择时已有效（弱市正超额），推荐B做精细化
        # 如果自然择时不足（弱市负超额），推荐C做补救
        # 如果状态差异极大且稳定，才考虑A

        if has_natural_timing and weak_excess_positive:
            recommendation = "B"
            reason = (
                "自然择时已存在且有效（弱市有正超额），但仍有精细化空间。\n"
                "方向B（状态敏感仓位）可在现有信号基础上做显式仓位映射：\n"
                "- 强牛：允许行业仓位上限提高至60%（当前约45%）\n"
                "- 弱牛：降低行业仓位至35%，增加防御比例\n"
                "- 震荡：维持40%行业仓位，保持灵活\n"
                "- 熊市：强制行业仓位不超过25%，防御不低于40%\n"
                "这比方向A（改参数）风险更小，比方向C（过滤信号）更不容易过拟合。"
            )
        elif not has_natural_timing or not weak_excess_positive:
            recommendation = "C"
            reason = (
                "自然择时不足或弱市保护不够。\n"
                "方向C（状态信号过滤）最直接：在熊市/震荡期提高信号门槛，\n"
                "减少低质量交易，增强防御配置。这能直接解决弱市跑输的问题。"
            )
        else:
            recommendation = "A"
            reason = (
                "状态间表现差异大，但自然择时已提供基础保护。\n"
                "方向A（状态参数映射）可尝试在不同状态下使用不同的均线周期、\n"
                "评分权重或止盈阈值。这需要更多实验但潜在收益最高。"
            )

        f.write(f"**推荐方向: {recommendation}**\n\n")
        f.write(f"{reason}\n\n")

        # 各方向评估
        f.write("### 5.3 三方向评估矩阵\n\n")
        f.write("| 方向 | 描述 | 实施复杂度 | 过拟合风险 | 预期增量 | 推荐优先级 |\n")
        f.write("|------|------|----------|----------|----------|----------|\n")
        f.write("| A | 状态→参数映射（不同状态用不同均线/阈值/权重） | 高 | 高 | 高 | 低 |\n")
        f.write("| B | 状态敏感仓位（动态调整行业/防御/现金比例） | 中 | 中 | 中 | **高** |\n")
        f.write("| C | 状态信号过滤（弱市提高BUY门槛） | 低 | 低 | 低-中 | 中 |\n")
        f.write("\n")

        f.write("## 6. 进入下一步实验的条件评估\n\n")
        f.write("| 条件 | 评估 | 说明 |\n")
        f.write("|------|------|------|\n")
        f.write(f"| 状态分布合理（无状态占比<5%） | {'OK' if all(s.get('days',0)/regime_summary.get('total_days',1) > 0.05 for s in regime_summary.get('state_distribution',{}).values()) else 'WARN'} | 各状态需有足够样本 |\n")
        f.write(f"| 状态间表现差异显著 | {'OK' if cagr_diff > 0.1 else 'WARN'} | CAGR差异>{10 if cagr_diff > 0.1 else '10%'}个百分点 |\n")
        f.write(f"| 自然择时不完全覆盖 | {'OK' if not (has_natural_timing and weak_excess_positive) else 'WARN'} | 仍有增量空间 |\n")
        f.write(f"| 状态切换频率适中 | {'OK' if regime_summary.get('switch_count',0) < 50 else 'WARN'} | 切换次数={regime_summary.get('switch_count',0)} |\n")
        f.write("\n")

        f.write("## 7. 免责声明\n\n")
        f.write("- 本分析基于B0.4回测数据，observer模式不改变交易逻辑。\n")
        f.write("- 状态检测使用沪深300指数，确认期5天，可能存在滞后。\n")
        f.write("- 分状态统计的样本量差异可能导致统计不显著（尤其强牛/弱牛天数较少时）。\n")
        f.write("- 方向推荐基于当前数据，实际实验需验证期确认。\n")

    print(f"  报告已保存: {output_md}")


def main():
    print("=" * 70)
    print("v1.3 Step 2: B0.4 市场状态增量价值诊断")
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

    # 2. 按样本划分计算
    print("\n[2/4] 按市场状态计算策略表现...")

    # 日期边界
    research_end = pd.to_datetime('2022-12-31')
    validation_start = pd.to_datetime('2023-01-01')
    validation_end = pd.to_datetime('2024-12-31')
    exclude_start = pd.to_datetime('2025-01-01')

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
                print(f"    {regime_name}: {s['days']}天, 策略CAGR={s['cagr']*100:.1f}%, "
                      f"基准CAGR={s['bench_cagr']*100:.1f}%, 超额={s['excess_cagr']*100:.1f}%, "
                      f"行业={s['avg_industry_pct']*100:.1f}%, 防御={s['avg_defense_pct']*100:.1f}%")
            else:
                print(f"    {regime_name}: 0天")

    # 3. 生成报告
    print("\n[3/4] 生成报告...")
    output_dir = os.path.join(BASE_DIR, 'reports')
    os.makedirs(output_dir, exist_ok=True)

    output_md = os.path.join(output_dir, 'v1_3_step2_regime_diagnosis.md')
    output_csv = os.path.join(output_dir, 'v1_3_step2_regime_stats.csv')

    generate_report(full_stats, nav_df, trades_df, regime_summary, output_md, output_csv)

    # 4. 数据勾稽
    print("\n[4/4] 数据勾稽...")
    total_days_check = sum(
        full_stats['全区间'][r]['days']
        for r in ['强牛', '弱牛', '震荡', '熊市']
        if full_stats['全区间'][r].get('days', 0) > 0
    )
    print(f"  四状态天数合计: {total_days_check} (应 ≈ {len(nav_df[full_mask])})")
    if abs(total_days_check - len(nav_df[full_mask])) > 5:
        print(f"  WARN: 天数差异: {total_days_check - len(nav_df[full_mask])}")
    else:
        print(f"  OK: 天数勾稽通过")

    # 交易数勾稽
    total_trades = sum(
        full_stats['全区间'][r].get('trade_count', 0)
        for r in ['强牛', '弱牛', '震荡', '熊市']
    )
    print(f"  四状态交易数合计: {total_trades} (应 ≈ {len(trades_df)})")
    if abs(total_trades - len(trades_df)) > 5:
        print(f"  WARN: 交易数差异: {total_trades - len(trades_df)} (可能因交易发生在非交易日)")
    else:
        print(f"  OK: 交易数勾稽通过")

    print("\n" + "=" * 70)
    print("v1.3 Step 2 完成")
    print("=" * 70)

    return {
        'full_stats': full_stats,
        'regime_summary': regime_summary,
    }


if __name__ == '__main__':
    main()
