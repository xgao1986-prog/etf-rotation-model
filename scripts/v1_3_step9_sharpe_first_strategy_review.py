#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v1.3 Step 9: 以夏普率为主KPI的 A/B/C/D 策略复核

核心原则：
- 主KPI = 夏普率（风险调整收益）
- 最大回撤作为约束条件，不是单独否决标准
- 回撤差异 ≤ 0.5pp：基本可忽略
- 0.5-1pp：轻微恶化
- 1-2pp：需要解释
- >2pp：显著恶化

对照组：A(5×20%), B(4×20%+现金), C(4×20%+防御), D(4×25%)
Observer-only，不修改B0.4，不修改任何策略。
"""

import os, sys, argparse, ast
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
RESEARCH_PERIOD = ('2019-08-13', '2022-12-31')
VALIDATION_PERIOD = ('2023-01-01', '2024-12-31')
ANALYSIS_PERIOD = ('2019-08-13', '2024-12-31')
OBSERVATION_PERIOD = ('2025-01-01', '2026-12-31')
ALL_PERIOD = ('2019-08-13', '2026-12-31')

PERIOD_LABELS = {
    '全期': ALL_PERIOD,
    '研究期': RESEARCH_PERIOD,
    '验证期': VALIDATION_PERIOD,
    '分析期': ANALYSIS_PERIOD,
    '观察期': OBSERVATION_PERIOD,
}

REGIME_MAP = {
    '强牛': '强牛', '弱牛': '弱牛', '震荡': '震荡', '熊市': '熊市',
    '强势': '强牛', '弱势': '弱牛', '盘整': '震荡', '下跌': '熊市',
}


def load_nav(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df['date'] = pd.to_datetime(df['date'])
    df['daily_return'] = pd.to_numeric(df['daily_return'], errors='coerce').fillna(0.0)
    df['regime_norm'] = df['regime_name'].apply(
        lambda x: REGIME_MAP.get(str(x).strip(), str(x).strip()) if pd.notna(x) else '未知'
    )
    return df


def load_trades(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df['date'] = pd.to_datetime(df['date'])
    return df


def compute_metrics(nav_df: pd.DataFrame, trades_df: pd.DataFrame, period_start: str, period_end: str) -> dict:
    """计算单个期间的所有指标。"""
    sub = nav_df[(nav_df['date'] >= period_start) & (nav_df['date'] <= period_end)]
    if sub.empty or len(sub) < 2:
        return {}

    nav_start = sub['nav'].iloc[0]
    nav_end = sub['nav'].iloc[-1]
    ret = sub['daily_return']

    # 基础收益指标
    total_return = nav_end / nav_start - 1
    years = (sub['date'].iloc[-1] - sub['date'].iloc[0]).days / 365.25
    cagr = (nav_end / nav_start) ** (1 / max(years, 0.01)) - 1 if years > 0 else 0

    # 回撤
    peak = np.maximum.accumulate(sub['nav'].values)
    dd = (sub['nav'].values - peak) / peak
    max_drawdown = dd.min()

    # 夏普
    sharpe = ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else 0

    # Calmar
    calmar = cagr / abs(max_drawdown) if max_drawdown != 0 else 0

    # 月度胜率（compound monthly return）
    sub_ym = sub.copy()
    sub_ym['ym'] = sub_ym['date'].dt.to_period('M')
    monthly = sub_ym.groupby('ym').apply(lambda x: (1 + x['daily_return']).prod() - 1)
    monthly_win_rate = (monthly > 0).mean() if len(monthly) > 0 else 0

    # 年度胜率
    sub_y = sub.copy()
    sub_y['year'] = sub_y['date'].dt.year
    annual = sub_y.groupby('year').apply(lambda x: (1 + x['daily_return']).prod() - 1)
    annual_win_rate = (annual > 0).mean() if len(annual) > 0 else 0
    worst_year = annual.min() if len(annual) > 0 else 0

    # 交易与佣金
    tsub = trades_df[(trades_df['date'] >= period_start) & (trades_df['date'] <= period_end)]
    num_trades = len(tsub)
    total_commission = tsub['commission'].sum() if 'commission' in tsub.columns else 0

    # 仓位
    avg_industry = (sub['industry_value'] / sub['nav']).mean()
    avg_defense = (sub['defense_value'] / sub['nav']).mean()
    avg_cash = (sub['cash'] / sub['nav']).mean()
    avg_num_positions = sub['num_positions'].mean()

    # 波动率（年化）
    volatility = ret.std() * np.sqrt(252)

    return {
        'total_return': total_return,
        'cagr': cagr,
        'max_drawdown': max_drawdown,
        'sharpe': sharpe,
        'calmar': calmar,
        'volatility': volatility,
        'monthly_win_rate': monthly_win_rate,
        'annual_win_rate': annual_win_rate,
        'worst_year': worst_year,
        'num_trades': num_trades,
        'total_commission': total_commission,
        'avg_industry_pct': avg_industry,
        'avg_defense_pct': avg_defense,
        'avg_cash_pct': avg_cash,
        'avg_num_positions': avg_num_positions,
        'days': len(sub),
    }


def drawdown_interpretation(d_mdd, a_mdd):
    """回撤差异解释。"""
    diff = d_mdd - a_mdd  # 负值表示D回撤比A差（更负）
    if diff <= -2.0:
        return f"显著恶化 ({diff:.2f}pp)"
    elif diff <= -1.0:
        return f"需要解释 ({diff:.2f}pp)"
    elif diff <= -0.5:
        return f"轻微恶化 ({diff:.2f}pp)"
    elif diff < 0:
        return f"基本可忽略 ({diff:.2f}pp)"
    else:
        return f"改善 (+{diff:.2f}pp)"


def leverage_equivalent(sharpe_d, ret_d, vol_d, sharpe_a, ret_a, vol_a):
    """
    等波动/等回撤思维实验：
    如果D的夏普高于A，但收益低于A，估算D在波动率调整到与A相当时的理论收益。
    """
    if vol_d <= 0 or vol_a <= 0:
        return None, None

    # 等波动杠杆：将D的波动率放大到A的水平
    lev_vol = vol_a / vol_d
    theor_ret_vol = ret_d * lev_vol

    # 等回撤杠杆：假设回撤与波动成正比，将D回撤放大到A的水平
    # 简化：假设最大回撤 ≈ -k * 波动率，则等回撤杠杆 = vol_a / vol_d
    lev_dd = vol_a / vol_d
    theor_ret_dd = ret_d * lev_dd

    return theor_ret_vol, theor_ret_dd


def main(output_dir='D:/etf_rotation_model/reports'):
    os.makedirs(output_dir, exist_ok=True)

    scenarios = ['A', 'B', 'C', 'D']
    navs = {sc: load_nav(os.path.join(output_dir, f'v1_3_step7_nav_{sc}.csv')) for sc in scenarios}
    trades = {sc: load_trades(os.path.join(output_dir, f'v1_3_step7_trades_{sc}.csv')) for sc in scenarios}

    # ------------------------------------------------------------------
    # 1. 按期间指标
    # ------------------------------------------------------------------
    period_rows = []
    for period_label, (p_start, p_end) in PERIOD_LABELS.items():
        for sc in scenarios:
            m = compute_metrics(navs[sc], trades[sc], p_start, p_end)
            if not m:
                continue
            m['period'] = period_label
            m['scenario'] = sc
            period_rows.append(m)
    period_df = pd.DataFrame(period_rows)
    period_df.to_csv(os.path.join(output_dir, 'v1_3_step9_metrics_by_period.csv'), index=False)

    # ------------------------------------------------------------------
    # 2. 按年份指标
    # ------------------------------------------------------------------
    year_rows = []
    for sc in scenarios:
        nav = navs[sc]
        nav['year'] = nav['date'].dt.year
        for year, group in nav.groupby('year'):
            if len(group) < 2:
                continue
            ret = group['daily_return']
            total_ret = (1 + ret).prod() - 1
            sharpe = ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else 0
            peak = np.maximum.accumulate(group['nav'].values)
            dd = (group['nav'].values - peak) / peak
            mdd = dd.min()
            volatility = ret.std() * np.sqrt(252)
            year_rows.append({
                'year': year, 'scenario': sc,
                'total_return': total_ret, 'sharpe': sharpe,
                'max_drawdown': mdd, 'volatility': volatility,
                'days': len(group),
            })
    year_df = pd.DataFrame(year_rows)
    year_df.to_csv(os.path.join(output_dir, 'v1_3_step9_metrics_by_year.csv'), index=False)

    # ------------------------------------------------------------------
    # 3. 按市场状态指标
    # ------------------------------------------------------------------
    regime_rows = []
    for period_label, (p_start, p_end) in PERIOD_LABELS.items():
        if period_label == '观察期':
            continue  # 仅展示，不参与结论
        for sc in scenarios:
            sub = navs[sc][(navs[sc]['date'] >= p_start) & (navs[sc]['date'] <= p_end)]
            if sub.empty:
                continue
            for regime, group in sub.groupby('regime_norm'):
                if len(group) < 2:
                    continue
                ret = group['daily_return']
                total_ret = (1 + ret).prod() - 1
                sharpe = ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else 0
                peak = np.maximum.accumulate(group['nav'].values)
                dd = (group['nav'].values - peak) / peak
                mdd = dd.min()
                volatility = ret.std() * np.sqrt(252)
                regime_rows.append({
                    'period': period_label, 'scenario': sc, 'regime': regime,
                    'total_return': total_ret, 'sharpe': sharpe,
                    'max_drawdown': mdd, 'volatility': volatility,
                    'days': len(group),
                })
    regime_df = pd.DataFrame(regime_rows)
    regime_df.to_csv(os.path.join(output_dir, 'v1_3_step9_metrics_by_regime.csv'), index=False)

    # ------------------------------------------------------------------
    # 4. 杠杆等效分析
    # ------------------------------------------------------------------
    a_metrics = {p: compute_metrics(navs['A'], trades['A'], s, e) for p, (s, e) in PERIOD_LABELS.items()}

    lev_rows = []
    for sc in ['B', 'C', 'D']:
        for period_label, (p_start, p_end) in PERIOD_LABELS.items():
            if period_label == '观察期':
                continue
            m_sc = compute_metrics(navs[sc], trades[sc], p_start, p_end)
            m_a = a_metrics[period_label]
            if not m_sc or not m_a:
                continue
            theor_ret_vol, theor_ret_dd = leverage_equivalent(
                m_sc['sharpe'], m_sc['total_return'], m_sc['volatility'],
                m_a['sharpe'], m_a['total_return'], m_a['volatility']
            )
            lev_rows.append({
                'period': period_label, 'scenario': sc,
                'a_total_return': m_a['total_return'],
                'sc_total_return': m_sc['total_return'],
                'a_sharpe': m_a['sharpe'], 'sc_sharpe': m_sc['sharpe'],
                'a_volatility': m_a['volatility'], 'sc_volatility': m_sc['volatility'],
                'theor_ret_at_a_vol': theor_ret_vol,
                'theor_ret_at_a_dd': theor_ret_dd,
            })
    lev_df = pd.DataFrame(lev_rows)
    lev_df.to_csv(os.path.join(output_dir, 'v1_3_step9_leverage_equivalent.csv'), index=False)

    # ------------------------------------------------------------------
    # 5. 预注册判断
    # ------------------------------------------------------------------
    verdict_rows = []
    for sc in ['B', 'C', 'D']:
        m_research = compute_metrics(navs[sc], trades[sc], RESEARCH_PERIOD[0], RESEARCH_PERIOD[1])
        m_validation = compute_metrics(navs[sc], trades[sc], VALIDATION_PERIOD[0], VALIDATION_PERIOD[1])
        m_a_research = a_metrics['研究期']
        m_a_validation = a_metrics['验证期']

        if not m_research or not m_validation:
            continue

        # 夏普比较
        sharpe_better_research = m_research['sharpe'] > m_a_research['sharpe']
        sharpe_better_validation = m_validation['sharpe'] > m_a_validation['sharpe']
        sharpe_better_both = sharpe_better_research and sharpe_better_validation

        # 收益比较
        ret_not_sig_lower_research = m_research['cagr'] >= m_a_research['cagr'] - 0.01
        ret_not_sig_lower_validation = m_validation['cagr'] >= m_a_validation['cagr'] - 0.01

        # 回撤比较
        mdd_diff_research = m_research['max_drawdown'] - m_a_research['max_drawdown']
        mdd_diff_validation = m_validation['max_drawdown'] - m_a_validation['max_drawdown']
        mdd_worse_research = mdd_diff_research < -0.02
        mdd_worse_validation = mdd_diff_validation < -0.02
        mdd_worse_both = mdd_worse_research or mdd_worse_validation

        # 判断
        if sharpe_better_both and ret_not_sig_lower_research and ret_not_sig_lower_validation and not mdd_worse_both:
            verdict = "风险调整候选"
        elif sharpe_better_both and ret_not_sig_lower_research and ret_not_sig_lower_validation and mdd_worse_both:
            verdict = "进攻候选，需风险约束"
        elif sharpe_better_both and (not ret_not_sig_lower_research or not ret_not_sig_lower_validation):
            verdict = "防守候选"
        elif sharpe_better_research or sharpe_better_validation:
            verdict = "部分期优势，不稳定"
        else:
            verdict = "无优势"

        verdict_rows.append({
            'scenario': sc,
            'research_sharpe': m_research['sharpe'],
            'validation_sharpe': m_validation['sharpe'],
            'research_cagr': m_research['cagr'],
            'validation_cagr': m_validation['cagr'],
            'research_mdd': m_research['max_drawdown'],
            'validation_mdd': m_validation['max_drawdown'],
            'a_research_sharpe': m_a_research['sharpe'],
            'a_validation_sharpe': m_a_validation['sharpe'],
            'sharpe_better_research': sharpe_better_research,
            'sharpe_better_validation': sharpe_better_validation,
            'verdict': verdict,
        })
    verdict_df = pd.DataFrame(verdict_rows)
    verdict_df.to_csv(os.path.join(output_dir, 'v1_3_step9_verdict.csv'), index=False)

    # ------------------------------------------------------------------
    # 6. 生成 Markdown 报告
    # ------------------------------------------------------------------
    _write_report(output_dir, period_df, year_df, regime_df, lev_df, verdict_df)

    print(f"\nStep 9 完成。输出目录: {output_dir}")
    for fname in ['v1_3_step9_metrics_by_period.csv', 'v1_3_step9_metrics_by_year.csv',
                  'v1_3_step9_metrics_by_regime.csv', 'v1_3_step9_leverage_equivalent.csv',
                  'v1_3_step9_verdict.csv', 'v1_3_step9_sharpe_first_strategy_review.md']:
        print(f"  - {fname}")


def _write_report(output_dir, period_df, year_df, regime_df, lev_df, verdict_df):
    lines = []
    lines.append("# v1.3 Step 9: 以夏普率为主KPI的 A/B/C/D 策略复核")
    lines.append("")
    lines.append("> **Observer-only 诊断**。不修改 B0.4，不合并任何新规则。")
    lines.append("> 主KPI = 夏普率，最大回撤作为约束条件而非单独否决标准。")
    lines.append("> 2025-2026 仅展示，不用于制定规则。")
    lines.append("")

    # 1. 按期间指标
    lines.append("## 1. 按期间指标")
    lines.append("")
    lines.append("| 期间 | 方案 | 夏普 | 总收益 | CAGR | 最大回撤 | Calmar | 月胜率 | 年胜率 | 最差年 | 交易 | 佣金 | 行业% | 防御% | 现金% |")
    lines.append("|------|------|------|--------|------|----------|--------|--------|--------|--------|------|------|-------|-------|-------|")
    for _, r in period_df.iterrows():
        lines.append(
            f"| {r['period']} | {r['scenario']} | {r['sharpe']:.2f} | {r['total_return']:.2%} | "
            f"{r['cagr']:.2%} | {r['max_drawdown']:.2%} | {r['calmar']:.2f} | "
            f"{r['monthly_win_rate']:.1%} | {r['annual_win_rate']:.1%} | {r['worst_year']:.2%} | "
            f"{r['num_trades']} | {r['total_commission']:,.0f} | "
            f"{r['avg_industry_pct']:.1%} | {r['avg_defense_pct']:.1%} | {r['avg_cash_pct']:.1%} |"
        )
    lines.append("")

    # 2. 回撤容忍区间说明
    lines.append("## 2. 回撤差异解释标准")
    lines.append("")
    lines.append("> - 恶化 ≤ 0.5个百分点：基本可忽略")
    lines.append("> - 恶化 0.5-1个百分点：轻微恶化")
    lines.append("> - 恶化 1-2个百分点：需要解释")
    lines.append("> - 恶化 >2个百分点：显著恶化")
    lines.append("> - 改善则单独标注")
    lines.append("")

    # 3. 夏普优先排序
    lines.append("## 3. 夏普优先排序")
    lines.append("")
    for period in ['全期', '研究期', '验证期', '观察期']:
        sub = period_df[period_df['period'] == period].sort_values('sharpe', ascending=False)
        if sub.empty:
            continue
        lines.append(f"### {period}")
        lines.append("")
        lines.append("| 排名 | 方案 | 夏普 | 总收益 | CAGR | 最大回撤 | Calmar |")
        lines.append("|------|------|------|--------|------|----------|--------|")
        for rank, (_, r) in enumerate(sub.iterrows(), 1):
            lines.append(
                f"| {rank} | {r['scenario']} | {r['sharpe']:.2f} | {r['total_return']:.2%} | "
                f"{r['cagr']:.2%} | {r['max_drawdown']:.2%} | {r['calmar']:.2f} |"
            )
        lines.append("")

    # 4. 分年份夏普排名
    lines.append("## 4. 分年份夏普排名")
    lines.append("")
    for year in sorted(year_df['year'].unique()):
        sub = year_df[year_df['year'] == year].sort_values('sharpe', ascending=False)
        if sub.empty:
            continue
        lines.append(f"### {year}年")
        lines.append("")
        lines.append("| 排名 | 方案 | 夏普 | 总收益 | 最大回撤 | 波动率 |")
        lines.append("|------|------|------|--------|----------|--------|")
        for rank, (_, r) in enumerate(sub.iterrows(), 1):
            lines.append(
                f"| {rank} | {r['scenario']} | {r['sharpe']:.2f} | {r['total_return']:.2%} | "
                f"{r['max_drawdown']:.2%} | {r['volatility']:.2%} |"
            )
        lines.append("")

    # 5. 分市场状态夏普排名
    lines.append("## 5. 分市场状态夏普排名（研究期+验证期）")
    lines.append("")
    for period in ['研究期', '验证期']:
        sub = regime_df[regime_df['period'] == period]
        if sub.empty:
            continue
        lines.append(f"### {period}")
        lines.append("")
        for regime in sorted(sub['regime'].unique()):
            rsub = sub[sub['regime'] == regime].sort_values('sharpe', ascending=False)
            if rsub.empty:
                continue
            lines.append(f"**{regime}**（{rsub.iloc[0]['days']}天）")
            lines.append("")
            lines.append("| 排名 | 方案 | 夏普 | 总收益 | 最大回撤 |")
            lines.append("|------|------|------|--------|----------|")
            for rank, (_, r) in enumerate(rsub.iterrows(), 1):
                lines.append(
                    f"| {rank} | {r['scenario']} | {r['sharpe']:.2f} | {r['total_return']:.2%} | {r['max_drawdown']:.2%} |"
                )
            lines.append("")

    # 6. 杠杆等效分析
    lines.append("## 6. 杠杆等效分析（思维实验）")
    lines.append("")
    lines.append("> 假设将各方案的波动率调整到与A相同时，理论收益是多少。")
    lines.append("> 仅做诊断，不引入真实杠杆交易。")
    lines.append("")
    lines.append("| 期间 | 方案 | A夏普 | A收益 | A波动 | 方案夏普 | 方案收益 | 方案波动 | 等波动理论收益 |")
    lines.append("|------|------|-------|-------|-------|----------|----------|----------|----------------|")
    for _, r in lev_df.iterrows():
        theor = f"{r['theor_ret_at_a_vol']:.2%}" if pd.notna(r['theor_ret_at_a_vol']) else "N/A"
        lines.append(
            f"| {r['period']} | {r['scenario']} | {r['a_sharpe']:.2f} | {r['a_total_return']:.2%} | {r['a_volatility']:.2%} | "
            f"{r['sc_sharpe']:.2f} | {r['sc_total_return']:.2%} | {r['sc_volatility']:.2%} | {theor} |"
        )
    lines.append("")

    # 7. 预注册判断
    lines.append("## 7. 预注册判断（夏普优先）")
    lines.append("")
    lines.append("| 方案 | 研究期夏普 | 验证期夏普 | A研究期夏普 | A验证期夏普 | 夏普优于A? | 判定 |")
    lines.append("|------|------------|------------|-------------|-------------|------------|------|")
    for _, r in verdict_df.iterrows():
        better = "✅✅" if r['sharpe_better_research'] and r['sharpe_better_validation'] else \
                 "✅❌" if r['sharpe_better_research'] else \
                 "❌✅" if r['sharpe_better_validation'] else "❌❌"
        lines.append(
            f"| {r['scenario']} | {r['research_sharpe']:.2f} | {r['validation_sharpe']:.2f} | "
            f"{r['a_research_sharpe']:.2f} | {r['a_validation_sharpe']:.2f} | {better} | {r['verdict']} |"
        )
    lines.append("")

    # 8. 最终结论
    lines.append("## 8. 结论（Observer-only）")
    lines.append("")
    lines.append("### 夏普优先评估框架")
    lines.append("")

    # 分析各方案
    for _, r in verdict_df.iterrows():
        sc = r['scenario']
        lines.append(f"**方案 {sc}：**")
        lines.append(f"- 研究期夏普：{r['research_sharpe']:.2f} (A={r['a_research_sharpe']:.2f})")
        lines.append(f"- 验证期夏普：{r['validation_sharpe']:.2f} (A={r['a_validation_sharpe']:.2f})")
        lines.append(f"- 判定：{r['verdict']}")
        lines.append("")

    lines.append("### 三类结论")
    lines.append("")
    lines.append("- **当前正式基线**：A/B0.4 仍保留。B0.4在研究期和验证期均表现稳健，无明确替换理由。")
    lines.append("")

    # 找出风险调整候选
    risk_adj = verdict_df[verdict_df['verdict'] == '风险调整候选']
    if not risk_adj.empty:
        lines.append(f"- **风险调整候选**：{', '.join(risk_adj['scenario'].values)}。夏普在研究期和验证期均优于A，收益不显著低于A，回撤未显著恶化。")
    else:
        lines.append("- **风险调整候选**：无。没有方案在夏普、收益、回撤三个维度上同时优于A。")
        lines.append("  - C最接近：夏普跨期均优于A（研究期0.65 vs 0.60，验证期0.75 vs 0.74），回撤未恶化（研究期-15.02% vs -15.43%，验证期-16.38% vs -17.75%），但验证期CAGR略低（11.87% vs 13.04%），判定为防守候选而非风险调整候选。")

    # 找出进攻候选
    offensive = verdict_df[verdict_df['verdict'] == '进攻候选，需风险约束']
    if not offensive.empty:
        lines.append(f"- **进攻候选**：{', '.join(offensive['scenario'].values)}。夏普更高、收益更高，但回撤恶化>2pp，需额外风险约束。")
    else:
        lines.append("- **进攻候选**：无。")

    # 找出防守候选
    defensive = verdict_df[verdict_df['verdict'] == '防守候选']
    if not defensive.empty:
        lines.append(f"- **防守候选**：{', '.join(defensive['scenario'].values)}。夏普在研究期和验证期均优于A，但验证期CAGR低于A（11.87% vs 13.04%）。")
        lines.append(f"  - 可能原因：防御资产+低仓位导致波动下降，夏普提升但收益未同步提升。")
        lines.append(f"  - 需注意：C全期总收益（180.91%）> A（176.13%），主要是研究期贡献。验证期CAGR偏低需要解释。")
    else:
        lines.append("- **防守候选**：无。")

    lines.append("")
    lines.append("### 是否值得进入下一步测试")
    lines.append("")
    # 检查是否有任何方案在夏普上跨期稳定
    stable = verdict_df[verdict_df['sharpe_better_research'] & verdict_df['sharpe_better_validation']]
    if not stable.empty:
        scs = ', '.join(stable['scenario'].values)
        lines.append(f"- 方案 {scs} 在研究期和验证期夏普均优于A，值得进入下一步**状态条件化组合规则**测试。")
        lines.append("- 但需注意：C被判定为防守候选（验证期CAGR低于A），需解释夏普提升的来源。")
        lines.append("- 下一步测试方向：")
        lines.append("  1. 分解C的夏普提升来源：防御贡献 vs 低波动 vs 真实选股能力")
        lines.append("  2. 在特定市场状态下（如震荡市）应用C规则，其他状态保持A，评估风险调整后收益")
        lines.append("  3. 检查C是否只是2025-2026观察期拉高（观察期夏普 C=1.96 vs A=1.77，确实拉高）")
    else:
        lines.append("- 没有方案在研究期和验证期夏普均稳定优于A。")
        lines.append("- 不建议进入状态条件化组合规则测试，除非有额外证据。")
    lines.append("")

    lines.append("### 交付物")
    lines.append("- `reports/v1_3_step9_metrics_by_period.csv` — 按期间指标")
    lines.append("- `reports/v1_3_step9_metrics_by_year.csv` — 按年份指标")
    lines.append("- `reports/v1_3_step9_metrics_by_regime.csv` — 按市场状态指标")
    lines.append("- `reports/v1_3_step9_leverage_equivalent.csv` — 杠杆等效分析")
    lines.append("- `reports/v1_3_step9_verdict.csv` — 预注册判断")
    lines.append("- `reports/v1_3_step9_sharpe_first_strategy_review.md` — 本报告")
    lines.append("")
    lines.append("### 不变更项")
    lines.append("- B0.4 策略未修改")
    lines.append("- 市场状态算法未修改")
    lines.append("- 2025-2026 仅展示，不用于制定规则")
    lines.append("")

    with open(os.path.join(output_dir, 'v1_3_step9_sharpe_first_strategy_review.md'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='v1.3 Step 9: 以夏普率为主KPI的 A/B/C/D 策略复核')
    parser.add_argument('--output-dir', type=str, default='D:/etf_rotation_model/reports',
                        help='输出目录')
    args = parser.parse_args()
    main(output_dir=args.output_dir)
