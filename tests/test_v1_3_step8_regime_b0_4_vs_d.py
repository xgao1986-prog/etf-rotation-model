#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v1.3 Step 8 测试：B0.4 vs D 市场状态分层诊断
"""

import os
import sys
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

REPORTS_DIR = os.path.join(os.path.dirname(__file__), '..', 'reports')


def test_regime_summary_csv_exists():
    """v1_3_step8_regime_summary.csv 必须存在。"""
    path = os.path.join(REPORTS_DIR, 'v1_3_step8_regime_summary.csv')
    assert os.path.exists(path), f"Missing: {path}"


def test_year_regime_matrix_csv_exists():
    """v1_3_step8_year_regime_matrix.csv 必须存在。"""
    path = os.path.join(REPORTS_DIR, 'v1_3_step8_year_regime_matrix.csv')
    assert os.path.exists(path), f"Missing: {path}"


def test_exposure_by_regime_csv_exists():
    """v1_3_step8_exposure_by_regime.csv 必须存在。"""
    path = os.path.join(REPORTS_DIR, 'v1_3_step8_exposure_by_regime.csv')
    assert os.path.exists(path), f"Missing: {path}"


def test_verdict_csv_exists():
    """v1_3_step8_verdict.csv 必须存在。"""
    path = os.path.join(REPORTS_DIR, 'v1_3_step8_verdict.csv')
    assert os.path.exists(path), f"Missing: {path}"


def test_report_md_exists():
    """v1_3_step8_regime_b0_4_vs_d.md 必须存在。"""
    path = os.path.join(REPORTS_DIR, 'v1_3_step8_regime_b0_4_vs_d.md')
    assert os.path.exists(path), f"Missing: {path}"


def test_regime_summary_has_all_periods():
    """regime_summary 必须包含研究期、验证期、观察期。"""
    df = pd.read_csv(os.path.join(REPORTS_DIR, 'v1_3_step8_regime_summary.csv'))
    assert '研究期' in df['period'].values, "Missing 研究期"
    assert '验证期' in df['period'].values, "Missing 验证期"
    assert '观察期' in df['period'].values, "Missing 观察期"


def test_regime_summary_has_expected_regimes():
    """regime_summary 必须包含主要状态（至少强牛、弱牛、震荡、熊市）。"""
    df = pd.read_csv(os.path.join(REPORTS_DIR, 'v1_3_step8_regime_summary.csv'))
    regimes = set(df['regime'].unique())
    expected = {'强牛', '弱牛', '震荡', '熊市'}
    assert expected.issubset(regimes), f"Missing regimes: {expected - regimes}"


def test_year_matrix_has_research_and_validation():
    """year_regime_matrix 必须包含研究期和验证期。"""
    df = pd.read_csv(os.path.join(REPORTS_DIR, 'v1_3_step8_year_regime_matrix.csv'))
    assert '研究期' in df['period'].values, "Missing 研究期"
    assert '验证期' in df['period'].values, "Missing 验证期"


def test_exposure_d_higher_industry_than_a():
    """D方案的行业暴露应该高于A方案（4×25% vs 5×20%）。"""
    df = pd.read_csv(os.path.join(REPORTS_DIR, 'v1_3_step8_exposure_by_regime.csv'))
    # 排除观察期（仅展示），检查研究期和验证期
    sub = df[df['period'] != '观察期']
    for _, row in sub.iterrows():
        assert row['d_avg_industry_pct'] >= row['a_avg_industry_pct'] - 0.05, \
            f"{row['period']} {row['regime']}: D industry {row['d_avg_industry_pct']:.1%} < A {row['a_avg_industry_pct']:.1%}"


def test_verdict_cross_period_consistency():
    """验证期和研究期必须有可对比的状态。"""
    df = pd.read_csv(os.path.join(REPORTS_DIR, 'v1_3_step8_verdict.csv'))
    research = df[df['period'] == '研究期']
    validation = df[df['period'] == '验证期']
    # 至少有一个状态在两期都出现
    common = set(research['regime'].unique()) & set(validation['regime'].unique())
    assert len(common) >= 1, f"研究期和验证期没有共同状态: research={research['regime'].unique()}, validation={validation['regime'].unique()}"


def test_verdict_has_risk_return_tradeoff():
    """verdict 中必须包含'风险换收益'判定（因为D集中度高）。"""
    df = pd.read_csv(os.path.join(REPORTS_DIR, 'v1_3_step8_verdict.csv'))
    assert '风险换收益' in df['verdict'].values, "verdict 中缺少'风险换收益'判定"


def test_report_disclaimer_observer_only():
    """报告必须包含 observer-only 免责声明。"""
    with open(os.path.join(REPORTS_DIR, 'v1_3_step8_regime_b0_4_vs_d.md'), 'r', encoding='utf-8') as f:
        text = f.read()
    assert 'Observer-only' in text, "报告缺少 Observer-only 声明"
    assert '不修改 B0.4' in text, "报告缺少'不修改 B0.4'声明"
    assert '2025-2026 仅展示' in text, "报告缺少'2025-2026 仅展示'声明"
