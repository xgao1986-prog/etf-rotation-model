#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v1.3 Step 10: 不同市场形态下 A/B/C/D 风险调整表现对比

Observer-only 诊断。不生成新交易规则，不修改 B0.4/A/B/C/D/市场状态算法。

样本分区：
- 研究期：2019-2022
- 验证期：2023-2024
- 观察期：2025-2026（仅展示，不参与规则选择）

核心指标：夏普率为主KPI，同时检查收益和回撤。
"""

import os, sys, argparse
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
from v1_3_step9_sharpe_first_strategy_review import load_nav, load_trades, compute_metrics


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

SMALL_SAMPLE_DAYS = 60  # 状态天数 < 60 标注小样本


# ---------------------------------------------------------------------------
# 核心：按 period × regime × scenario 计算指标
# ---------------------------------------------------------------------------
def compute_regime_metrics(nav_df, trades_df, period_start, period_end, risk_free_rate=0.0):
    """
    返回一个 dict，key=(period_label, regime, scenario)，value=metrics dict。
    使用完整连续日收益映射到状态，而不是先筛日期再 pct_change。
    """
    sub = nav_df[(nav_df['date'] >= period_start) & (nav_df['date'] <= period_end)]
    if sub.empty or len(sub) < 2:
        return {}

    rows = []
    for regime, group in sub.groupby('regime_norm'):
        if len(group) < 2:
            continue

        # 使用 group 自身的完整连续日收益计算指标
        nav_start = group['nav'].iloc[0]
        nav_end = group['nav'].iloc[-1]
        ret = group['daily_return']
        total_return = nav_end / nav_start - 1
        years = (group['date'].iloc[-1] - group['date'].iloc[0]).days / 365.25
        cagr = (nav_end / nav_start) ** (1 / max(years, 0.01)) - 1 if years > 0 else 0

        peak = np.maximum.accumulate(group['nav'].values)
        dd = (group['nav'].values - peak) / peak
        max_drawdown = dd.min()

        rf_daily = risk_free_rate / 252
        sharpe = ((ret.mean() - rf_daily) / ret.std() * np.sqrt(252)) if ret.std() > 0 else 0
        calmar = cagr / abs(max_drawdown) if max_drawdown != 0 else 0
        volatility = ret.std() * np.sqrt(252)

        # 月度胜率
        group_ym = group.copy()
        group_ym['ym'] = group_ym['date'].dt.to_period('M')
        monthly = group_ym.groupby('ym').apply(lambda x: (1 + x['daily_return']).prod() - 1)
        monthly_win_rate = (monthly > 0).mean() if len(monthly) > 0 else 0

        # 仓位
        avg_industry = (group['industry_value'] / group['nav']).mean()
        avg_defense = (group['defense_value'] / group['nav']).mean()
        avg_cash = (group['cash'] / group['nav']).mean()
        avg_num_positions = group['num_positions'].mean()

        # 交易与佣金
        tsub = trades_df[(trades_df['date'] >= group['date'].iloc[0]) & (trades_df['date'] <= group['date'].iloc[-1])]
        num_trades = len(tsub)
        total_commission = tsub['commission'].sum() if 'commission' in tsub.columns else 0

        rows.append({
            'period': '',  # 填充时由外层提供
            'regime': regime,
            'total_return': total_return,
            'cagr': cagr,
            'max_drawdown': max_drawdown,
            'sharpe': sharpe,
            'calmar': calmar,
            'volatility': volatility,
            'monthly_win_rate': monthly_win_rate,
            'avg_industry_pct': avg_industry,
            'avg_defense_pct': avg_defense,
            'avg_cash_pct': avg_cash,
            'avg_num_positions': avg_num_positions,
            'num_trades': num_trades,
            'total_commission': total_commission,
            'days': len(group),
        })
    return rows


# ---------------------------------------------------------------------------
# 综合评分
# ---------------------------------------------------------------------------
def compute_composite_score(sub_df):
    """
    在同一 regime 的 A/B/C/D 中横向计算综合评分。
    评分权重：夏普 50%，收益 25%，回撤 20%，成本 5%。
    返回带 composite_score 的 DataFrame。
    """
    if sub_df.empty or len(sub_df) < 2:
        sub_df = sub_df.copy()
        sub_df['composite_score'] = np.nan
        return sub_df

    # 归一化辅助函数
    def _norm(series, higher_better=True):
        s = series.fillna(0)
        rng = s.max() - s.min()
        if rng == 0:
            return pd.Series(0.5, index=s.index)
        if higher_better:
            return (s - s.min()) / rng
        else:
            return (s.max() - s) / rng

    sharpe_norm = _norm(sub_df['sharpe'], higher_better=True)
    ret_norm = _norm(sub_df['total_return'], higher_better=True)
    # 回撤：max_drawdown 是负值，绝对值越小越好 → 用 |dd| 的反向归一化
    dd_abs = sub_df['max_drawdown'].abs()
    dd_norm = _norm(dd_abs, higher_better=False)
    # 成本：交易次数+佣金，越少越好
    cost = sub_df['num_trades'] + sub_df['total_commission'] / 1000.0  # 简单综合
    cost_norm = _norm(cost, higher_better=False)

    composite = 0.50 * sharpe_norm + 0.25 * ret_norm + 0.20 * dd_norm + 0.05 * cost_norm
    sub_df = sub_df.copy()
    sub_df['composite_score'] = composite.values
    sub_df['sharpe_rank'] = sub_df['sharpe'].rank(ascending=False).astype(int)
    sub_df['composite_rank'] = sub_df['composite_score'].rank(ascending=False).astype(int)
    return sub_df


# ---------------------------------------------------------------------------
# 跨期稳定性：判断某方案在 regime 下是否跨期优于 A
# ---------------------------------------------------------------------------
def check_cross_period_stability(df, regime, scenario):
    """
    df 是研究期+验证期的 metrics_by_regime。
    检查 scenario 在 regime 下研究期和验证期的夏普是否均优于 A。
    """
    research = df[(df['period'] == '研究期') & (df['regime'] == regime)]
    validation = df[(df['period'] == '验证期') & (df['regime'] == regime)]

    a_research = research[research['scenario'] == 'A']
    a_validation = validation[validation['scenario'] == 'A']
    sc_research = research[research['scenario'] == scenario]
    sc_validation = validation[validation['scenario'] == scenario]

    if a_research.empty or a_validation.empty or sc_research.empty or sc_validation.empty:
        return False, '缺少数据'

    a_research_sharpe = a_research.iloc[0]['sharpe']
    a_validation_sharpe = a_validation.iloc[0]['sharpe']
    sc_research_sharpe = sc_research.iloc[0]['sharpe']
    sc_validation_sharpe = sc_validation.iloc[0]['sharpe']

    a_research_cagr = a_research.iloc[0]['cagr']
    a_validation_cagr = a_validation.iloc[0]['cagr']
    sc_research_cagr = sc_research.iloc[0]['cagr']
    sc_validation_cagr = sc_validation.iloc[0]['cagr']

    a_research_mdd = a_research.iloc[0]['max_drawdown']
    a_validation_mdd = a_validation.iloc[0]['max_drawdown']
    sc_research_mdd = sc_research.iloc[0]['max_drawdown']
    sc_validation_mdd = sc_validation.iloc[0]['max_drawdown']

    # 条件
    sharpe_better_both = (sc_research_sharpe > a_research_sharpe) and (sc_validation_sharpe > a_validation_sharpe)
    ret_not_sig_lower_research = sc_research_cagr >= a_research_cagr - 0.01
    ret_not_sig_lower_validation = sc_validation_cagr >= a_validation_cagr - 0.01
    mdd_not_worse_research = (sc_research_mdd - a_research_mdd) >= -0.02
    mdd_not_worse_validation = (sc_validation_mdd - a_validation_mdd) >= -0.02

    if not sharpe_better_both:
        return False, '夏普未跨期均优于A'
    if not (ret_not_sig_lower_research and ret_not_sig_lower_validation):
        return False, '收益显著低于A'
    if not (mdd_not_worse_research and mdd_not_worse_validation):
        return False, '回撤恶化超过2pp'

    return True, '跨期稳定优于A'


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------
def main(output_dir='D:/etf_rotation_model/reports', risk_free_rate=0.0):
    os.makedirs(output_dir, exist_ok=True)

    scenarios = ['A', 'B', 'C', 'D']
    navs = {sc: load_nav(os.path.join(output_dir, f'v1_3_step7_nav_{sc}.csv')) for sc in scenarios}
    trades = {sc: load_trades(os.path.join(output_dir, f'v1_3_step7_trades_{sc}.csv')) for sc in scenarios}

    # ------------------------------------------------------------------
    # 1. 按 period × regime × scenario 计算指标
    # ------------------------------------------------------------------
    regime_rows = []
    for period_label, (p_start, p_end) in PERIOD_LABELS.items():
        for sc in scenarios:
            rows = compute_regime_metrics(navs[sc], trades[sc], p_start, p_end, risk_free_rate=risk_free_rate)
            for r in rows:
                r['period'] = period_label
                r['scenario'] = sc
            regime_rows.extend(rows)

    metrics_df = pd.DataFrame(regime_rows)
    metrics_df.to_csv(os.path.join(output_dir, 'v1_3_step10_metrics_by_regime.csv'), index=False)

    # ------------------------------------------------------------------
    # 2. 按 regime 横向排名（夏普 + 综合评分）
    # ------------------------------------------------------------------
    rank_rows = []
    for period in ['研究期', '验证期', '观察期']:
        sub = metrics_df[metrics_df['period'] == period]
        for regime in sorted(sub['regime'].unique()):
            rsub = sub[sub['regime'] == regime].copy()
            if rsub.empty or len(rsub) < 2:
                continue
            # 按 scenario 补齐（某些 scenario 可能无数据）
            rsub = compute_composite_score(rsub)
            for _, r in rsub.iterrows():
                rank_rows.append({
                    'period': period,
                    'regime': regime,
                    'scenario': r['scenario'],
                    'sharpe': r['sharpe'],
                    'sharpe_rank': r['sharpe_rank'],
                    'composite_score': r['composite_score'],
                    'composite_rank': r['composite_rank'],
                    'total_return': r['total_return'],
                    'max_drawdown': r['max_drawdown'],
                    'days': r['days'],
                })

    rank_df = pd.DataFrame(rank_rows)
    rank_df.to_csv(os.path.join(output_dir, 'v1_3_step10_rank_by_regime.csv'), index=False)

    # ------------------------------------------------------------------
    # 3. 小样本标注
    # ------------------------------------------------------------------
    small_flags = []
    for _, r in metrics_df.iterrows():
        if r['days'] < SMALL_SAMPLE_DAYS:
            small_flags.append({
                'period': r['period'],
                'regime': r['regime'],
                'scenario': r['scenario'],
                'days': r['days'],
                'flag': '小样本（<60天）',
            })
    small_df = pd.DataFrame(small_flags)
    small_df.to_csv(os.path.join(output_dir, 'v1_3_step10_small_sample_flags.csv'), index=False)

    # ------------------------------------------------------------------
    # 4. 候选方案矩阵（仅研究期+验证期）
    # ------------------------------------------------------------------
    candidate_rows = []
    # 只研究研究期和验证期数据
    analysis_df = metrics_df[metrics_df['period'].isin(['研究期', '验证期'])]

    # 获取所有在研究期或验证期出现过的 regime
    all_regimes = sorted(analysis_df['regime'].unique())

    for regime in all_regimes:
        # 研究期数据
        research = analysis_df[(analysis_df['period'] == '研究期') & (analysis_df['regime'] == regime)]
        validation = analysis_df[(analysis_df['period'] == '验证期') & (analysis_df['regime'] == regime)]

        # 检查天数是否满足小样本
        research_days = research['days'].max() if not research.empty else 0
        validation_days = validation['days'].max() if not validation.empty else 0
        small_sample = (research_days < SMALL_SAMPLE_DAYS) or (validation_days < SMALL_SAMPLE_DAYS)

        candidates = []
        reasons = []
        for sc in ['B', 'C', 'D']:
            ok, reason = check_cross_period_stability(analysis_df, regime, sc)
            if ok:
                candidates.append(sc)
                reasons.append(f'{sc}: {reason}')

        candidate_rows.append({
            'regime': regime,
            'research_days': research_days,
            'validation_days': validation_days,
            'small_sample': small_sample,
            'candidate': ' / '.join(candidates) if candidates else '不建议切换',
            'reason': '；'.join(reasons) if reasons else '无方案跨期稳定优于A',
        })

    candidate_df = pd.DataFrame(candidate_rows)
    candidate_df.to_csv(os.path.join(output_dir, 'v1_3_step10_candidate_matrix.csv'), index=False)

    # ------------------------------------------------------------------
    # 5. 生成 Markdown 报告
    # ------------------------------------------------------------------
    _write_report(output_dir, metrics_df, rank_df, candidate_df, small_df, risk_free_rate=risk_free_rate)

    print(f"\nStep 10 完成。输出目录: {output_dir}")
    for fname in ['v1_3_step10_metrics_by_regime.csv',
                  'v1_3_step10_rank_by_regime.csv',
                  'v1_3_step10_small_sample_flags.csv',
                  'v1_3_step10_candidate_matrix.csv',
                  'v1_3_step10_regime_abcd_comparison.md']:
        print(f"  - {fname}")


# ---------------------------------------------------------------------------
# 报告生成
# ---------------------------------------------------------------------------
def _write_report(output_dir, metrics_df, rank_df, candidate_df, small_df, risk_free_rate=0.0):
    lines = []
    lines.append("# v1.3 Step 10: 不同市场形态下 A/B/C/D 风险调整表现对比")
    lines.append("")
    lines.append("> **Observer-only 诊断**。不生成新交易规则，不修改 B0.4/A/B/C/D/市场状态算法。")
    lines.append("> 本阶段只是找'状态条件化候选'，不能直接进入实盘。")
    lines.append("> 下一步才是把候选矩阵转成可交易规则做完整 A/B 测试。")
    if risk_free_rate > 0:
        lines.append(f"> 无风险收益率 = {risk_free_rate:.2%}（已纳入夏普计算）。")
    else:
        lines.append("> 无风险收益率 = 0%（默认，未纳入夏普计算）。")
    lines.append("")

    # 1. 各状态指标表
    lines.append("## 1. 各市场状态下指标（按 period × regime × scenario）")
    lines.append("")
    lines.append("| 期间 | 状态 | 方案 | 天数 | 总收益 | CAGR | 夏普 | 最大回撤 | Calmar | 月胜率 | 行业% | 防御% | 现金% | 持仓数 | 交易 | 佣金 |")
    lines.append("|------|------|------|------|--------|------|------|----------|--------|--------|-------|-------|-------|--------|------|------|")
    for _, r in metrics_df.iterrows():
        small_badge = " ⚠️小样本" if r['days'] < SMALL_SAMPLE_DAYS else ""
        lines.append(
            f"| {r['period']} | {r['regime']}{small_badge} | {r['scenario']} | {r['days']} | "
            f"{r['total_return']:.2%} | {r['cagr']:.2%} | {r['sharpe']:.2f} | {r['max_drawdown']:.2%} | "
            f"{r['calmar']:.2f} | {r['monthly_win_rate']:.1%} | {r['avg_industry_pct']:.1%} | "
            f"{r['avg_defense_pct']:.1%} | {r['avg_cash_pct']:.1%} | {r['avg_num_positions']:.1f} | "
            f"{r['num_trades']} | {r['total_commission']:,.0f} |"
        )
    lines.append("")

    # 2. 夏普率排名
    lines.append("## 2. 每个状态下按夏普率排名")
    lines.append("")
    for period in ['研究期', '验证期', '观察期']:
        sub = rank_df[rank_df['period'] == period]
        if sub.empty:
            continue
        lines.append(f"### {period}")
        lines.append("")
        for regime in sorted(sub['regime'].unique()):
            rsub = sub[sub['regime'] == regime].sort_values('sharpe_rank')
            if rsub.empty:
                continue
            small_badge = " ⚠️小样本" if rsub.iloc[0]['days'] < SMALL_SAMPLE_DAYS else ""
            lines.append(f"**{regime}**{small_badge}")
            lines.append("")
            lines.append("| 排名 | 方案 | 夏普 | 总收益 | 最大回撤 | 天数 |")
            lines.append("|------|------|------|--------|----------|------|")
            for _, r in rsub.iterrows():
                lines.append(
                    f"| {r['sharpe_rank']} | {r['scenario']} | {r['sharpe']:.2f} | "
                    f"{r['total_return']:.2%} | {r['max_drawdown']:.2%} | {r['days']} |"
                )
            lines.append("")

    # 3. 综合评分排名
    lines.append("## 3. 每个状态下按综合评分排名")
    lines.append("")
    lines.append("> 综合评分权重：夏普率 50%，收益 25%，最大回撤 20%，成本/换手 5%。")
    lines.append("")
    for period in ['研究期', '验证期', '观察期']:
        sub = rank_df[rank_df['period'] == period]
        if sub.empty:
            continue
        lines.append(f"### {period}")
        lines.append("")
        for regime in sorted(sub['regime'].unique()):
            rsub = sub[sub['regime'] == regime].sort_values('composite_rank')
            if rsub.empty:
                continue
            small_badge = " ⚠️小样本" if rsub.iloc[0]['days'] < SMALL_SAMPLE_DAYS else ""
            lines.append(f"**{regime}**{small_badge}")
            lines.append("")
            lines.append("| 排名 | 方案 | 综合评分 | 夏普 | 总收益 | 最大回撤 | 天数 |")
            lines.append("|------|------|----------|------|--------|----------|------|")
            for _, r in rsub.iterrows():
                lines.append(
                    f"| {r['composite_rank']} | {r['scenario']} | {r['composite_score']:.3f} | "
                    f"{r['sharpe']:.2f} | {r['total_return']:.2%} | {r['max_drawdown']:.2%} | {r['days']} |"
                )
            lines.append("")

    # 4. 跨期稳定性排名
    lines.append("## 4. 跨期稳定性排名（研究期+验证期）")
    lines.append("")
    lines.append("> 候选条件：研究期和验证期同一状态下，夏普率均优于 A，收益不显著低于 A，回撤恶化不超过 2pp。")
    lines.append("")
    lines.append("| 状态 | 研究期天数 | 验证期天数 | 小样本? | 候选方案 | 原因 |")
    lines.append("|------|------------|------------|---------|----------|------|")
    for _, r in candidate_df.iterrows():
        small = "⚠️是" if r['small_sample'] else "否"
        lines.append(
            f"| {r['regime']} | {r['research_days']} | {r['validation_days']} | {small} | "
            f"{r['candidate']} | {r['reason']} |"
        )
    lines.append("")

    # 5. 小样本标注
    lines.append("## 5. 小样本标注（天数 < 60）")
    lines.append("")
    if small_df.empty:
        lines.append("无小样本状态。")
    else:
        lines.append("| 期间 | 状态 | 方案 | 天数 | 标注 |")
        lines.append("|------|------|------|------|------|")
        for _, r in small_df.iterrows():
            lines.append(f"| {r['period']} | {r['regime']} | {r['scenario']} | {r['days']} | {r['flag']} |")
    lines.append("")

    # 6. 状态 → 候选方案矩阵
    lines.append("## 6. 状态 → 候选方案矩阵")
    lines.append("")
    lines.append("> 这只是候选矩阵，不是最终交易规则。")
    lines.append("")
    lines.append("| 状态 | 候选方案 | 说明 |")
    lines.append("|------|----------|------|")
    for _, r in candidate_df.iterrows():
        small = "⚠️小样本，" if r['small_sample'] else ""
        lines.append(f"| {r['regime']} | {r['candidate']} | {small}{r['reason']} |")
    lines.append("")

    # 7. 结论
    lines.append("## 7. 结论（Observer-only）")
    lines.append("")
    lines.append("### 当前正式基线")
    lines.append("")
    lines.append("- **B0.4 仍是当前正式基线**。A（5×20%）= B0.4。")
    lines.append("")

    lines.append("### 状态条件化候选发现")
    lines.append("")
    has_candidate = candidate_df[candidate_df['candidate'] != '不建议切换']
    if has_candidate.empty:
        lines.append("- **本次未发现任何状态条件化候选方案**。")
        lines.append("- 没有方案在任何市场状态下，研究期和验证期夏普均稳定优于 A。")
        lines.append("- 不建议进入状态条件化组合规则测试。")
    else:
        for regime in sorted(has_candidate['regime'].unique()):
            sub = has_candidate[has_candidate['regime'] == regime]
            cands = ', '.join(sub['candidate'].values)
            lines.append(f"- **{regime}**：候选方案 {cands}。")
        lines.append("")
        lines.append("- 但需注意：以上候选仅满足'跨期稳定性'最低门槛，不代表可以直接进入实盘。")
    lines.append("")

    lines.append("### 下一步")
    lines.append("")
    lines.append("- 如果候选矩阵非空，下一步是：**把候选矩阵转成可交易规则，做完整 A/B 测试**。")
    lines.append("- 如果候选矩阵为空，说明在当前 A/B/C/D 方案池中，没有状态条件化收益。")
    lines.append("- 需要回到 Step 7/8/9 进一步寻找更好的方案变体。")
    lines.append("")

    lines.append("### 交付物")
    lines.append("- `reports/v1_3_step10_metrics_by_regime.csv` — 各状态指标")
    lines.append("- `reports/v1_3_step10_rank_by_regime.csv` — 夏普排名 + 综合评分排名")
    lines.append("- `reports/v1_3_step10_small_sample_flags.csv` — 小样本标注")
    lines.append("- `reports/v1_3_step10_candidate_matrix.csv` — 候选方案矩阵")
    lines.append("- `reports/v1_3_step10_regime_abcd_comparison.md` — 本报告")
    lines.append("")

    lines.append("### 不变更项")
    lines.append("- B0.4 策略未修改")
    lines.append("- A/B/C/D 方案未修改")
    lines.append("- 市场状态算法未修改")
    lines.append("- 2025-2026 仅展示，不用于制定规则")
    lines.append("")

    with open(os.path.join(output_dir, 'v1_3_step10_regime_abcd_comparison.md'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='v1.3 Step 10: 不同市场形态下 A/B/C/D 风险调整表现对比')
    parser.add_argument('--output-dir', type=str, default='D:/etf_rotation_model/reports',
                        help='输出目录')
    parser.add_argument('--risk-free-rate', type=float, default=0.0,
                        help='无风险收益率（年化），默认0，不纳入夏普计算')
    args = parser.parse_args()
    main(output_dir=args.output_dir, risk_free_rate=args.risk_free_rate)
