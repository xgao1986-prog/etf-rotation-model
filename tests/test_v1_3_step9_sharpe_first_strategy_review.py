#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_v1_3_step9_sharpe_first_strategy_review.py

覆盖 Step 9 的 8 项交付物 CSV + Markdown 报告验证。
"""

import os, sys, pytest
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from v1_3_step9_sharpe_first_strategy_review import (
    compute_metrics, main, _write_report, drawdown_interpretation, leverage_equivalent
)

REPORTS_DIR = os.path.join(os.path.dirname(__file__), '..', 'reports')


class TestCsvDeliverables:
    """P0-2: 验证所有 CSV 交付物存在且字段完整。"""

    def test_metrics_by_period_exists(self):
        path = os.path.join(REPORTS_DIR, 'v1_3_step9_metrics_by_period.csv')
        assert os.path.exists(path), f"Missing {path}"

    def test_metrics_by_year_exists(self):
        path = os.path.join(REPORTS_DIR, 'v1_3_step9_metrics_by_year.csv')
        assert os.path.exists(path), f"Missing {path}"

    def test_metrics_by_regime_exists(self):
        path = os.path.join(REPORTS_DIR, 'v1_3_step9_metrics_by_regime.csv')
        assert os.path.exists(path), f"Missing {path}"

    def test_leverage_equivalent_exists(self):
        path = os.path.join(REPORTS_DIR, 'v1_3_step9_leverage_equivalent.csv')
        assert os.path.exists(path), f"Missing {path}"

    def test_verdict_exists(self):
        path = os.path.join(REPORTS_DIR, 'v1_3_step9_verdict.csv')
        assert os.path.exists(path), f"Missing {path}"

    def test_metrics_by_period_fields(self):
        df = pd.read_csv(os.path.join(REPORTS_DIR, 'v1_3_step9_metrics_by_period.csv'))
        required = {'period', 'scenario', 'total_return', 'cagr', 'max_drawdown',
                    'sharpe', 'calmar', 'volatility', 'monthly_win_rate',
                    'annual_win_rate', 'worst_year', 'num_trades',
                    'total_commission', 'avg_industry_pct', 'avg_defense_pct',
                    'avg_cash_pct', 'avg_num_positions', 'days'}
        assert required.issubset(set(df.columns)), f"Missing fields: {required - set(df.columns)}"

    def test_metrics_by_year_fields(self):
        df = pd.read_csv(os.path.join(REPORTS_DIR, 'v1_3_step9_metrics_by_year.csv'))
        required = {'year', 'scenario', 'total_return', 'sharpe', 'max_drawdown', 'volatility', 'days'}
        assert required.issubset(set(df.columns)), f"Missing fields: {required - set(df.columns)}"

    def test_metrics_by_regime_fields(self):
        df = pd.read_csv(os.path.join(REPORTS_DIR, 'v1_3_step9_metrics_by_regime.csv'))
        required = {'period', 'scenario', 'regime', 'total_return', 'sharpe', 'max_drawdown', 'volatility', 'days'}
        assert required.issubset(set(df.columns)), f"Missing fields: {required - set(df.columns)}"

    def test_leverage_equivalent_fields(self):
        df = pd.read_csv(os.path.join(REPORTS_DIR, 'v1_3_step9_leverage_equivalent.csv'))
        required = {'period', 'scenario', 'a_total_return', 'sc_total_return',
                    'a_sharpe', 'sc_sharpe', 'a_volatility', 'sc_volatility',
                    'theor_ret_at_a_vol', 'theor_ret_at_a_dd'}
        assert required.issubset(set(df.columns)), f"Missing fields: {required - set(df.columns)}"

    def test_verdict_fields(self):
        df = pd.read_csv(os.path.join(REPORTS_DIR, 'v1_3_step9_verdict.csv'))
        required = {'scenario', 'research_sharpe', 'validation_sharpe',
                    'research_cagr', 'validation_cagr',
                    'research_mdd', 'validation_mdd',
                    'a_research_sharpe', 'a_validation_sharpe',
                    'sharpe_better_research', 'sharpe_better_validation', 'verdict'}
        assert required.issubset(set(df.columns)), f"Missing fields: {required - set(df.columns)}"


class TestSharpeVerdictLogic:
    """P0-2: 验证夏普 verdict 逻辑。"""

    def test_all_scenarios_present(self):
        df = pd.read_csv(os.path.join(REPORTS_DIR, 'v1_3_step9_verdict.csv'))
        assert set(df['scenario'].values) == {'B', 'C', 'D'}, "Must have B, C, D"

    def test_sharpe_better_flags_consistent(self):
        df = pd.read_csv(os.path.join(REPORTS_DIR, 'v1_3_step9_verdict.csv'))
        for _, r in df.iterrows():
            assert r['sharpe_better_research'] == (r['research_sharpe'] > r['a_research_sharpe']), \
                f"research_sharpe flag inconsistent for {r['scenario']}"
            assert r['sharpe_better_validation'] == (r['validation_sharpe'] > r['a_validation_sharpe']), \
                f"validation_sharpe flag inconsistent for {r['scenario']}"

    def test_verdict_categories_valid(self):
        df = pd.read_csv(os.path.join(REPORTS_DIR, 'v1_3_step9_verdict.csv'))
        valid = {'风险调整候选', '进攻候选，需风险约束', '防守候选', '部分期优势，不稳定', '无优势'}
        assert set(df['verdict'].unique()).issubset(valid), f"Invalid verdicts: {set(df['verdict'].unique()) - valid}"

    def test_c_verdict_is_defensive(self):
        df = pd.read_csv(os.path.join(REPORTS_DIR, 'v1_3_step9_verdict.csv'))
        c = df[df['scenario'] == 'C']
        assert not c.empty, "C must be present"
        assert c.iloc[0]['verdict'] == '防守候选', \
            f"C should be '防守候选' but got {c.iloc[0]['verdict']}"


class TestReportMarkdown:
    """P0-1: 报告 Markdown 格式完整性。"""

    @pytest.fixture(scope='class')
    def report_lines(self):
        path = os.path.join(REPORTS_DIR, 'v1_3_step9_sharpe_first_strategy_review.md')
        with open(path, 'r', encoding='utf-8') as f:
            return f.read().splitlines()

    def test_all_sections_present(self, report_lines):
        headers = [l for l in report_lines if l.startswith('## ')]
        expected = ['## 1. 按期间指标', '## 2. 回撤差异解释标准',
                    '## 3. 夏普优先排序', '## 4. 分年份夏普排名',
                    '## 5. 分市场状态夏普排名（研究期+验证期）',
                    '## 6. 杠杆等效分析（思维实验）',
                    '## 7. 预注册判断（夏普优先）',
                    '## 8. 结论（Observer-only）']
        assert headers == expected, f"Section order mismatch:\nGot: {headers}\nExpected: {expected}"

    def test_no_section_4_6_interleaving(self, report_lines):
        """第4节和第6节之间必须有第5节，不能交错。"""
        idx_4 = next((i for i, l in enumerate(report_lines) if l == '## 4. 分年份夏普排名'), None)
        idx_5 = next((i for i, l in enumerate(report_lines) if l == '## 5. 分市场状态夏普排名（研究期+验证期）'), None)
        idx_6 = next((i for i, l in enumerate(report_lines) if l == '## 6. 杠杆等效分析（思维实验）'), None)
        assert idx_4 is not None and idx_5 is not None and idx_6 is not None
        assert idx_4 < idx_5 < idx_6, f"Section order wrong: 4@{idx_4}, 5@{idx_5}, 6@{idx_6}"

    def test_risk_free_rate_disclaimer(self, report_lines):
        """报告头部必须包含无风险收益率说明。"""
        text = '\n'.join(report_lines[:10])
        assert '无风险收益率' in text, "Missing risk-free-rate disclaimer in header"


class TestRiskFreeRate:
    """P0-3: 无风险收益率参数。"""

    def test_compute_metrics_accepts_rf(self):
        import pandas as pd
        dates = pd.date_range('2020-01-01', periods=252, freq='B')
        nav = pd.DataFrame({
            'date': dates,
            'nav': [100.0 + i * 0.1 for i in range(252)],
            'daily_return': [0.0005] * 252,
            'regime_name': ['强牛'] * 252,
            'industry_value': [80.0] * 252,
            'defense_value': [0.0] * 252,
            'cash': [20.0] * 252,
            'num_positions': [5] * 252,
        })
        trades = pd.DataFrame({'date': dates[:10], 'commission': [10.0] * 10})
        m0 = compute_metrics(nav, trades, '2020-01-01', '2020-12-31', risk_free_rate=0.0)
        m3 = compute_metrics(nav, trades, '2020-01-01', '2020-12-31', risk_free_rate=0.03)
        assert m0['sharpe'] > m3['sharpe'], "Positive rf should reduce Sharpe"

    def test_default_rf_is_zero(self):
        import pandas as pd
        dates = pd.date_range('2020-01-01', periods=252, freq='B')
        nav = pd.DataFrame({
            'date': dates,
            'nav': [100.0 + i * 0.1 for i in range(252)],
            'daily_return': [0.0005] * 252,
            'regime_name': ['强牛'] * 252,
            'industry_value': [80.0] * 252,
            'defense_value': [0.0] * 252,
            'cash': [20.0] * 252,
            'num_positions': [5] * 252,
        })
        trades = pd.DataFrame({'date': dates[:10], 'commission': [10.0] * 10})
        m = compute_metrics(nav, trades, '2020-01-01', '2020-12-31')
        assert 'sharpe' in m and m['sharpe'] >= 0


class TestDataSanity:
    """数据合理性检查。"""

    def test_period_df_has_all_periods(self):
        df = pd.read_csv(os.path.join(REPORTS_DIR, 'v1_3_step9_metrics_by_period.csv'))
        periods = set(df['period'].unique())
        assert {'全期', '研究期', '验证期', '分析期', '观察期'}.issubset(periods), \
            f"Missing periods: {periods}"

    def test_all_scenarios_in_period_df(self):
        df = pd.read_csv(os.path.join(REPORTS_DIR, 'v1_3_step9_metrics_by_period.csv'))
        for p in ['全期', '研究期', '验证期', '分析期', '观察期']:
            sub = df[df['period'] == p]['scenario']
            assert set(sub) == {'A', 'B', 'C', 'D'}, f"Missing scenarios in period {p}"

    def test_sharpe_finite(self):
        df = pd.read_csv(os.path.join(REPORTS_DIR, 'v1_3_step9_metrics_by_period.csv'))
        assert df['sharpe'].notna().all(), "Sharpe must be finite"
        assert (df['sharpe'] != float('inf')).all(), "Sharpe must not be inf"
        assert (df['sharpe'] != float('-inf')).all(), "Sharpe must not be -inf"

    def test_max_drawdown_negative(self):
        df = pd.read_csv(os.path.join(REPORTS_DIR, 'v1_3_step9_metrics_by_period.csv'))
        assert (df['max_drawdown'] <= 0).all(), "Max drawdown should be <= 0"

    def test_leverage_equivalent_theoretical_sanity(self):
        df = pd.read_csv(os.path.join(REPORTS_DIR, 'v1_3_step9_leverage_equivalent.csv'))
        for _, r in df.iterrows():
            if pd.notna(r['theor_ret_at_a_vol']):
                assert -1.0 <= r['theor_ret_at_a_vol'] <= 10.0, \
                    f"theor_ret_at_a_vol out of bounds: {r['theor_ret_at_a_vol']}"
