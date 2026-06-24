#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v1.3 Step 8: B0.4 vs D 市场状态分层诊断
Observer-only，不修改B0.4，不合并规则，仅做诊断观察。

对照组：
  A = B0.4 (5×20%)
  D = 4×25% 行业集中，防御关闭

按 regime_name（强牛/弱牛/震荡/熊市）分层，分研究期/验证期/观察期三段。
"""

import os
import sys
import argparse
import pandas as pd
import numpy as np
import ast
from datetime import datetime

# 允许从 scripts/ 运行
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
RESEARCH_PERIOD = ('2019-08-13', '2022-12-31')
VALIDATION_PERIOD = ('2023-01-01', '2024-12-31')
OBSERVATION_PERIOD = ('2025-01-01', '2026-12-31')

PERIOD_LABELS = {
    '研究期': RESEARCH_PERIOD,
    '验证期': VALIDATION_PERIOD,
    '观察期': OBSERVATION_PERIOD,
}

# 状态映射：归一化为 4 大类
REGIME_MAP = {
    '强牛': '强牛',
    '弱牛': '弱牛',
    '震荡': '震荡',
    '熊市': '熊市',
    # 兼容可能的变体
    '强势': '强牛',
    '弱势': '弱牛',
    '盘整': '震荡',
    '下跌': '熊市',
}


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def load_nav(path: str) -> pd.DataFrame:
    """加载 nav CSV，解析日期和 positions_pct。"""
    df = pd.read_csv(path)
    df['date'] = pd.to_datetime(df['date'])
    # 解析 positions_pct（字符串 dict）
    df['positions_pct'] = df['positions_pct'].apply(
        lambda x: ast.literal_eval(x) if isinstance(x, str) and x.strip() else {}
    )
    # daily_return 可能在空字符串时缺失，fillna
    df['daily_return'] = pd.to_numeric(df['daily_return'], errors='coerce')
    df['daily_return'] = df['daily_return'].fillna(0.0)
    # 归一化 regime_name
    df['regime_norm'] = df['regime_name'].apply(
        lambda x: REGIME_MAP.get(str(x).strip(), str(x).strip()) if pd.notna(x) else '未知'
    )
    return df


def compute_period_return(df: pd.DataFrame, start: str, end: str) -> float:
    """计算期间累计收益（compound）。"""
    sub = df[(df['date'] >= start) & (df['date'] <= end)]
    if sub.empty:
        return 0.0
    return (1 + sub['daily_return']).prod() - 1


def compute_max_drawdown(df: pd.DataFrame, start: str, end: str) -> float:
    """计算期间最大回撤（基于 NAV）。"""
    sub = df[(df['date'] >= start) & (df['date'] <= end)]['nav'].values
    if len(sub) < 2:
        return 0.0
    peak = np.maximum.accumulate(sub)
    dd = (sub - peak) / peak
    return dd.min()


def compute_sharpe(df: pd.DataFrame, start: str, end: str) -> float:
    """计算期间夏普（日收益年化，无风险利率0）。"""
    sub = df[(df['date'] >= start) & (df['date'] <= end)]['daily_return']
    if sub.empty or sub.std() == 0:
        return 0.0
    return sub.mean() / sub.std() * np.sqrt(252)


def compute_avg_exposure(df: pd.DataFrame, start: str, end: str) -> dict:
    """计算期间平均敞口。"""
    sub = df[(df['date'] >= start) & (df['date'] <= end)]
    if sub.empty:
        return {}
    return {
        'avg_industry_pct': sub['industry_value'].mean() / sub['nav'].mean(),
        'avg_defense_pct': sub['defense_value'].mean() / sub['nav'].mean(),
        'avg_cash_pct': sub['cash'].mean() / sub['nav'].mean(),
        'avg_num_positions': sub['num_positions'].mean(),
    }


def regime_stats(df: pd.DataFrame, period_label: str, period_range: tuple) -> pd.DataFrame:
    """
    按状态分组统计（只统计该期间内的数据）。
    返回 DataFrame，每行一个 regime。
    """
    sub = df[(df['date'] >= period_range[0]) & (df['date'] <= period_range[1])].copy()
    if sub.empty:
        return pd.DataFrame()

    rows = []
    for regime, group in sub.groupby('regime_norm'):
        if len(group) < 2:
            continue
        # compound return
        ret = (1 + group['daily_return']).prod() - 1
        # max drawdown
        nav_vals = group['nav'].values
        peak = np.maximum.accumulate(nav_vals)
        dd = (nav_vals - peak) / peak
        mdd = dd.min()
        # sharpe
        sharpe = (group['daily_return'].mean() / group['daily_return'].std() * np.sqrt(252)
                  if group['daily_return'].std() > 0 else 0.0)
        # exposure
        avg_industry = group['industry_value'].mean() / group['nav'].mean()
        avg_defense = group['defense_value'].mean() / group['nav'].mean()
        avg_cash = group['cash'].mean() / group['nav'].mean()
        avg_num_pos = group['num_positions'].mean()

        rows.append({
            'period': period_label,
            'regime': regime,
            'days': len(group),
            'return': ret,
            'max_drawdown': mdd,
            'sharpe': sharpe,
            'avg_industry_pct': avg_industry,
            'avg_defense_pct': avg_defense,
            'avg_cash_pct': avg_cash,
            'avg_num_positions': avg_num_pos,
        })
    return pd.DataFrame(rows)


def year_regime_matrix(df_a: pd.DataFrame, df_d: pd.DataFrame, period_label: str, period_range: tuple) -> pd.DataFrame:
    """
    年份 × 状态 二维表。
    """
    sub_a = df_a[(df_a['date'] >= period_range[0]) & (df_a['date'] <= period_range[1])].copy()
    sub_d = df_d[(df_d['date'] >= period_range[0]) & (df_d['date'] <= period_range[1])].copy()

    sub_a['year'] = sub_a['date'].dt.year
    sub_d['year'] = sub_d['date'].dt.year

    rows = []
    for year in sorted(sub_a['year'].unique()):
        for regime in sorted(sub_a['regime_norm'].unique()):
            a_group = sub_a[(sub_a['year'] == year) & (sub_a['regime_norm'] == regime)]
            d_group = sub_d[(sub_d['year'] == year) & (sub_d['regime_norm'] == regime)]
            if len(a_group) < 2:
                continue
            a_ret = (1 + a_group['daily_return']).prod() - 1
            d_ret = (1 + d_group['daily_return']).prod() - 1 if len(d_group) >= 2 else 0.0
            diff = d_ret - a_ret
            rows.append({
                'period': period_label,
                'year': year,
                'regime': regime,
                'a_days': len(a_group),
                'd_days': len(d_group),
                'a_return': a_ret,
                'd_return': d_ret,
                'd_minus_a': diff,
            })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main(output_dir='D:/etf_rotation_model/reports'):
    os.makedirs(output_dir, exist_ok=True)

    nav_a = load_nav(os.path.join(output_dir, 'v1_3_step7_nav_A.csv'))
    nav_d = load_nav(os.path.join(output_dir, 'v1_3_step7_nav_D.csv'))

    # ---- 1. 分状态收益对比 ----
    regime_summary = []
    for period_label, period_range in PERIOD_LABELS.items():
        stats_a = regime_stats(nav_a, period_label, period_range)
        stats_d = regime_stats(nav_d, period_label, period_range)

        if stats_a.empty or stats_d.empty:
            continue

        merged = stats_a.merge(
            stats_d,
            on=['period', 'regime'],
            suffixes=('_a', '_d'),
            how='outer'
        )
        merged['d_minus_a_return'] = merged['return_d'] - merged['return_a']
        merged['d_minus_a_mdd'] = merged['max_drawdown_d'] - merged['max_drawdown_a']
        merged['d_minus_a_sharpe'] = merged['sharpe_d'] - merged['sharpe_a']
        merged['d_better'] = (merged['return_d'] > merged['return_a']).astype(int)

        # 补充：D是否因为集中而收益更高 / 回撤更大
        merged['d_higher_exposure'] = (merged['avg_industry_pct_d'] > merged['avg_industry_pct_a']).astype(int)
        merged['d_less_defense'] = (merged['avg_defense_pct_d'] < merged['avg_defense_pct_a']).astype(int)
        merged['d_fewer_positions'] = (merged['avg_num_positions_d'] < merged['avg_num_positions_a']).astype(int)

        regime_summary.append(merged)

    regime_summary_df = pd.concat(regime_summary, ignore_index=True) if regime_summary else pd.DataFrame()
    regime_summary_df.to_csv(
        os.path.join(output_dir, 'v1_3_step8_regime_summary.csv'), index=False
    )

    # ---- 2. 年份 × 状态 二维表 ----
    year_matrix = []
    for period_label, period_range in PERIOD_LABELS.items():
        mat = year_regime_matrix(nav_a, nav_d, period_label, period_range)
        if not mat.empty:
            year_matrix.append(mat)
    year_matrix_df = pd.concat(year_matrix, ignore_index=True) if year_matrix else pd.DataFrame()
    year_matrix_df.to_csv(
        os.path.join(output_dir, 'v1_3_step8_year_regime_matrix.csv'), index=False
    )

    # ---- 3. 持仓与风险暴露（每个状态） ----
    exposure_rows = []
    for period_label, period_range in PERIOD_LABELS.items():
        for regime in sorted(nav_a['regime_norm'].unique()):
            a_sub = nav_a[(nav_a['date'] >= period_range[0]) & (nav_a['date'] <= period_range[1])
                          & (nav_a['regime_norm'] == regime)]
            d_sub = nav_d[(nav_d['date'] >= period_range[0]) & (nav_d['date'] <= period_range[1])
                          & (nav_d['regime_norm'] == regime)]
            if len(a_sub) < 2:
                continue
            exposure_rows.append({
                'period': period_label,
                'regime': regime,
                'a_days': len(a_sub),
                'd_days': len(d_sub),
                'a_avg_industry_pct': a_sub['industry_value'].mean() / a_sub['nav'].mean(),
                'd_avg_industry_pct': d_sub['industry_value'].mean() / d_sub['nav'].mean() if len(d_sub) > 0 else 0.0,
                'a_avg_defense_pct': a_sub['defense_value'].mean() / a_sub['nav'].mean(),
                'd_avg_defense_pct': d_sub['defense_value'].mean() / d_sub['nav'].mean() if len(d_sub) > 0 else 0.0,
                'a_avg_cash_pct': a_sub['cash'].mean() / a_sub['nav'].mean(),
                'd_avg_cash_pct': d_sub['cash'].mean() / d_sub['nav'].mean() if len(d_sub) > 0 else 0.0,
                'a_avg_num_positions': a_sub['num_positions'].mean(),
                'd_avg_num_positions': d_sub['num_positions'].mean() if len(d_sub) > 0 else 0.0,
                'd_industry_minus_a': (d_sub['industry_value'].mean() / d_sub['nav'].mean() if len(d_sub) > 0 else 0.0)
                                      - (a_sub['industry_value'].mean() / a_sub['nav'].mean()),
                'd_defense_minus_a': (d_sub['defense_value'].mean() / d_sub['nav'].mean() if len(d_sub) > 0 else 0.0)
                                      - (a_sub['defense_value'].mean() / a_sub['nav'].mean()),
            })
    exposure_df = pd.DataFrame(exposure_rows)
    exposure_df.to_csv(
        os.path.join(output_dir, 'v1_3_step8_exposure_by_regime.csv'), index=False
    )

    # ---- 4. 预注册判断 ----
    verdict = []
    # 只考虑研究期和验证期
    for period in ['研究期', '验证期']:
        period_stats = regime_summary_df[regime_summary_df['period'] == period]
        if period_stats.empty:
            continue
        for regime in sorted(period_stats['regime'].unique()):
            row = period_stats[period_stats['regime'] == regime]
            if row.empty:
                continue
            row = row.iloc[0]
            verdict.append({
                'period': period,
                'regime': regime,
                'a_return': row['return_a'],
                'd_return': row['return_d'],
                'd_minus_a': row['d_minus_a_return'],
                'a_mdd': row['max_drawdown_a'],
                'd_mdd': row['max_drawdown_d'],
                'd_mdd_minus_a': row['d_minus_a_mdd'],
                'a_sharpe': row['sharpe_a'],
                'd_sharpe': row['sharpe_d'],
                'd_sharpe_minus_a': row['d_minus_a_sharpe'],
                'verdict': _verdict(row),
            })
    verdict_df = pd.DataFrame(verdict)
    verdict_df.to_csv(
        os.path.join(output_dir, 'v1_3_step8_verdict.csv'), index=False
    )

    # ---- 5. 生成 Markdown 报告 ----
    _write_report(output_dir, regime_summary_df, year_matrix_df, exposure_df, verdict_df)

    print(f"\nStep 8 完成。输出目录: {output_dir}")
    print(f"  - v1_3_step8_regime_summary.csv")
    print(f"  - v1_3_step8_year_regime_matrix.csv")
    print(f"  - v1_3_step8_exposure_by_regime.csv")
    print(f"  - v1_3_step8_verdict.csv")
    print(f"  - v1_3_step8_regime_b0_4_vs_d.md")


def _verdict(row) -> str:
    """根据规则判断该状态下 D 是否优于 A。"""
    d_ret = row['return_d']
    a_ret = row['return_a']
    d_mdd = row['max_drawdown_d']
    a_mdd = row['max_drawdown_a']
    d_sharpe = row['sharpe_d']
    a_sharpe = row['sharpe_a']

    if d_ret > a_ret and d_mdd >= a_mdd and d_sharpe > a_sharpe:
        return "明确改善"
    elif d_ret > a_ret and d_mdd >= a_mdd:
        return "收益改善但夏普未提升"
    elif d_ret > a_ret and d_mdd < a_mdd:
        return "风险换收益"
    elif d_ret > a_ret:
        return "收益略优但回撤恶化"
    elif d_ret <= a_ret and d_mdd < a_mdd:
        return "全面劣势"
    else:
        return "无优势"


def _write_report(output_dir, regime_summary_df, year_matrix_df, exposure_df, verdict_df):
    lines = []
    lines.append("# v1.3 Step 8: B0.4 vs D 市场状态分层诊断")
    lines.append("")
    lines.append("> **Observer-only 诊断**。不修改 B0.4，不合并任何新规则。")
    lines.append("> 当前只是诊断，不是策略合并。")
    lines.append("> 不修改市场状态算法。")
    lines.append("> 2025-2026 仅展示，不用于制定规则。")
    lines.append("")

    # 1. 分状态收益对比
    lines.append("## 1. 分状态收益对比")
    lines.append("")
    for period in ['研究期', '验证期', '观察期']:
        sub = regime_summary_df[regime_summary_df['period'] == period]
        if sub.empty:
            continue
        lines.append(f"### {period}")
        lines.append("")
        lines.append("| 状态 | 天数 | A收益 | D收益 | D-A | A回撤 | D回撤 | A夏普 | D夏普 | D相对A判定 |")
        lines.append("|------|------|-------|-------|-----|-------|-------|-------|-------|------------|")
        for _, r in sub.iterrows():
            # 使用 verdict 而不是简单的 ✅❌
            # 只要D回撤比A差，就标为风险换收益
            if r['d_better'] == 1:
                if r['d_minus_a_mdd'] < 0:  # D回撤比A差（任何程度）
                    verdict_icon = "⚠️ 风险换收益"
                elif r['d_minus_a_sharpe'] < 0:
                    verdict_icon = "⚠️ 收益改善但夏普未提升"
                else:
                    verdict_icon = "✅ 明确改善"
            else:
                if r['d_minus_a_mdd'] < 0:
                    verdict_icon = "❌ 全面劣势"
                else:
                    verdict_icon = "❌ 无优势"
            lines.append(
                f"| {r['regime']} | {r['days_a']} | "
                f"{r['return_a']:.2%} | {r['return_d']:.2%} | {r['d_minus_a_return']:.2%} | "
                f"{r['max_drawdown_a']:.2%} | {r['max_drawdown_d']:.2%} | "
                f"{r['sharpe_a']:.2f} | {r['sharpe_d']:.2f} | {verdict_icon} |"
            )
        lines.append("")

    # 2. 差异来源拆解
    lines.append("## 2. 差异来源拆解")
    lines.append("")
    lines.append("| 期间 | 状态 | D集中度高? | D防御更少? | D持仓更少? | 解释 |")
    lines.append("|------|------|------------|------------|------------|------|")
    for _, r in regime_summary_df.iterrows():
        high_exp = "是" if r['d_higher_exposure'] == 1 else "否"
        less_def = "是" if r['d_less_defense'] == 1 else "否"
        fewer_pos = "是" if r['d_fewer_positions'] == 1 else "否"
        explanation = []
        if r['d_higher_exposure'] == 1:
            explanation.append("行业暴露更高")
        if r['d_less_defense'] == 1:
            explanation.append("防御更少")
        if r['d_fewer_positions'] == 1:
            explanation.append("持仓更集中")
        if not explanation:
            explanation.append("敞口结构相似")
        lines.append(
            f"| {r['period']} | {r['regime']} | {high_exp} | {less_def} | {fewer_pos} | {'; '.join(explanation)} |"
        )
    lines.append("")

    # 3. 持仓与风险暴露
    lines.append("## 3. 持仓与风险暴露")
    lines.append("")
    for period in ['研究期', '验证期', '观察期']:
        sub = exposure_df[exposure_df['period'] == period]
        if sub.empty:
            continue
        lines.append(f"### {period}")
        lines.append("")
        lines.append("| 状态 | A行业% | D行业% | A防御% | D防御% | A现金% | D现金% | A只数 | D只数 | D相对A |")
        lines.append("|------|--------|--------|--------|--------|--------|--------|-------|-------|--------|")
        for _, r in sub.iterrows():
            d_rel = []
            if r['d_industry_minus_a'] > 0.01:
                d_rel.append("股票暴露更高")
            elif r['d_industry_minus_a'] < -0.01:
                d_rel.append("股票暴露更低")
            if r['d_defense_minus_a'] < -0.01:
                d_rel.append("防御更少")
            if not d_rel:
                d_rel.append("敞口相似")
            lines.append(
                f"| {r['regime']} | {r['a_avg_industry_pct']:.1%} | {r['d_avg_industry_pct']:.1%} | "
                f"{r['a_avg_defense_pct']:.1%} | {r['d_avg_defense_pct']:.1%} | "
                f"{r['a_avg_cash_pct']:.1%} | {r['d_avg_cash_pct']:.1%} | "
                f"{r['a_avg_num_positions']:.1f} | {r['d_avg_num_positions']:.1f} | {'; '.join(d_rel)} |"
            )
        lines.append("")

    # 4. 年份 × 状态二维表
    lines.append("## 4. 年份 × 状态 二维表")
    lines.append("")
    for period in ['研究期', '验证期', '观察期']:
        sub = year_matrix_df[year_matrix_df['period'] == period]
        if sub.empty:
            continue
        lines.append(f"### {period}")
        lines.append("")
        # Pivot: rows=year, columns=regime, values=d_minus_a
        pivot = sub.pivot(index='year', columns='regime', values='d_minus_a')
        if not pivot.empty:
            lines.append("| year | " + " | ".join(pivot.columns) + " |")
            lines.append("|------|" + "|".join(["--------"] * len(pivot.columns)) + "|")
            for year, row in pivot.iterrows():
                vals = " | ".join([f"{v:.2%}" if pd.notna(v) else "-" for v in row.values])
                lines.append(f"| {year} | {vals} |")
        lines.append("")

    # 5. 预注册判断
    lines.append("## 5. 预注册判断（Observer-only）")
    lines.append("")
    lines.append("> 判断标准：")
    lines.append("> - 若 D 在研究期和验证期的同一状态下都优于A，才认为该状态支持D")
    lines.append("> - 若 D 只在研究期优于A、验证期不优于A，判为不稳定")
    lines.append("> - 若 D收益更高但回撤显著恶化，判为风险换收益，不算明确改善")
    lines.append("> - 若 D优势主要来自2025-2026，不能作为规则依据")
    lines.append("")
    lines.append("| 期间 | 状态 | A收益 | D收益 | D-A | A回撤 | D回撤 | A夏普 | D夏普 | 判定 |")
    lines.append("|------|------|-------|-------|-----|-------|-------|-------|-------|------|")
    for _, r in verdict_df.iterrows():
        lines.append(
            f"| {r['period']} | {r['regime']} | {r['a_return']:.2%} | {r['d_return']:.2%} | "
            f"{r['d_minus_a']:.2%} | {r['a_mdd']:.2%} | {r['d_mdd']:.2%} | "
            f"{r['a_sharpe']:.2f} | {r['d_sharpe']:.2f} | {r['verdict']} |"
        )
    lines.append("")

    # 跨期一致性检查（降级表述）
    lines.append("### 跨期一致性")
    lines.append("")
    lines.append("> **注意**：跨期收益方向一致 ≠ 风险通过。只有当收益方向一致且回撤未恶化时，才能视为支持D。")
    lines.append("")
    research = verdict_df[verdict_df['period'] == '研究期']
    validation = verdict_df[verdict_df['period'] == '验证期']
    if not research.empty and not validation.empty:
        for regime in sorted(research['regime'].unique()):
            r_row = research[research['regime'] == regime]
            v_row = validation[validation['regime'] == regime]
            if r_row.empty or v_row.empty:
                continue
            r = r_row.iloc[0]
            v = v_row.iloc[0]
            r_better = r['d_return'] > r['a_return']
            v_better = v['d_return'] > v['a_return']
            r_mdd_worse = r['d_mdd'] < r['a_mdd']  # D回撤比A差（任何程度）
            v_mdd_worse = v['d_mdd'] < v['a_mdd']
            if r_better and v_better:
                if r_mdd_worse or v_mdd_worse:
                    lines.append(f"- **{regime}**: 研究期收益✅ 验证期收益✅，但研究期回撤{'恶化' if r_mdd_worse else '未恶化'}、验证期回撤{'恶化' if v_mdd_worse else '未恶化'} → **⚠️ 风险换收益型候选，不能视为明确改善**")
                else:
                    lines.append(f"- **{regime}**: 研究期✅ 验证期✅，回撤未恶化 → **跨期一致支持D**")
            elif r_better and not v_better:
                lines.append(f"- **{regime}**: 研究期收益✅ 验证期收益❌ → **不稳定，验证期不支持**")
            elif not r_better and v_better:
                lines.append(f"- **{regime}**: 研究期收益❌ 验证期收益✅ → **验证期反超，研究期不支持**")
            else:
                lines.append(f"- **{regime}**: 研究期收益❌ 验证期收益❌ → **D不优于A**")
    lines.append("")

    # 结论
    lines.append("## 6. 结论（Observer-only）")
    lines.append("")
    lines.append("> **免责声明**：以下结论仅为诊断观察，不意味着D可以替代B0.4。")
    lines.append("> 不修改B0.4，不合并任何新规则。")
    lines.append("")
    lines.append("### 核心发现")
    lines.append("")
    lines.append("- **弱牛**：研究期D-A=+2.20%，验证期D-A=-0.01% → ❌ 不稳定，验证期不支持")
    lines.append("- **强牛**：研究期D-A=+0.91%，验证期D-A=-0.18% → ❌ 不稳定，验证期不支持")
    lines.append("- **熊市**：研究期D-A=-2.73%，验证期D-A=-0.14% → ❌ D不优于A，集中仓位在下跌市场更脆弱")
    lines.append("- **震荡**：研究期D-A=+2.70%（回撤恶化），验证期D-A=+0.11%（回撤恶化） → ⚠️ 收益方向跨期一致，但风险未通过，只能视为**风险换收益型候选**，不能视为明确改善，也不能直接推出震荡市应用D")
    lines.append("")
    lines.append("### 预注册判断修正")
    lines.append("")
    lines.append("- 震荡市：收益方向跨期一致（研究期+2.70%，验证期+0.11%），但两期均伴随回撤恶化（研究期-15.58% vs -15.13%，验证期-3.65% vs -3.42%）")
    lines.append("- 因此**不满足'明确改善'**标准（要求收益改善且回撤不恶化）")
    lines.append("- 震荡市 D 只能进入下一步候选测试：震荡市 D 规则 + 风险约束（如止损收紧、仓位上限）")
    lines.append("- **不能直接合并为策略规则**，需额外验证风险调整后是否仍有效")
    lines.append("")
    lines.append("### 交付物")
    lines.append("- `reports/v1_3_step8_regime_summary.csv` — 分状态收益对比")
    lines.append("- `reports/v1_3_step8_year_regime_matrix.csv` — 年份×状态二维表")
    lines.append("- `reports/v1_3_step8_exposure_by_regime.csv` — 持仓与风险暴露")
    lines.append("- `reports/v1_3_step8_verdict.csv` — 预注册判断")
    lines.append("- `reports/v1_3_step8_regime_b0_4_vs_d.md` — 本报告")
    lines.append("")
    lines.append("### 不变更项")
    lines.append("- B0.4 策略未修改")
    lines.append("- 市场状态算法未修改")
    lines.append("- 2025-2026 仅展示，不用于制定规则")
    lines.append("")

    with open(os.path.join(output_dir, 'v1_3_step8_regime_b0_4_vs_d.md'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='v1.3 Step 8: B0.4 vs D 市场状态分层诊断')
    parser.add_argument('--output-dir', type=str, default='D:/etf_rotation_model/reports',
                        help='输出目录')
    args = parser.parse_args()
    main(output_dir=args.output_dir)
