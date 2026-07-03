#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_paper_trading_metrics.py — performance and comparison metrics."""

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from paper_trading.metrics import (
    calculate_account_metrics,
    calculate_closed_trade_win_rate,
    build_account_comparison,
)


def _nav_series(values, start_date='2026-01-01'):
    from datetime import datetime, timedelta
    dates = []
    d = datetime.strptime(start_date, '%Y-%m-%d')
    for i, v in enumerate(values):
        dates.append((d + timedelta(days=i)).strftime('%Y-%m-%d'))
    return [
        {'nav_date': date, 'nav': nav, 'cash': 0.0, 'positions_value': nav}
        for date, nav in zip(dates, values)
    ]


def test_cumulative_return_on_known_series():
    nav = _nav_series([1_000_000, 1_100_000, 1_200_000])
    metrics = calculate_account_metrics(nav, [])
    assert metrics['total_return'] == pytest.approx(0.20, abs=0.001)


def test_annualized_return_on_one_year_series():
    nav = _nav_series([1_000_000] + [1_000_000] * 252 + [1_200_000], start_date='2026-01-01')
    metrics = calculate_account_metrics(nav, [])
    assert metrics['annualized_return'] == pytest.approx(0.20, abs=0.02)


def test_maximum_drawdown_on_known_series():
    nav = _nav_series([1_000_000, 1_200_000, 900_000, 1_100_000])
    metrics = calculate_account_metrics(nav, [])
    assert metrics['max_drawdown'] == pytest.approx(-0.25, abs=0.001)


def test_sharpe_and_calmar_on_growth_with_drawdown():
    # 252 trading days, 50% total return with a visible drawdown
    values = [1_000_000 + 50_000 * i / 252 for i in range(253)]
    values[-1] = 1_500_000
    values[100] = values[100] * 0.90  # insert a 10% drawdown
    nav = _nav_series(values, start_date='2026-01-01')
    metrics = calculate_account_metrics(nav, [], risk_free_rate=0.03)
    assert metrics['sharpe'] > 0
    assert metrics['calmar'] > 0


def test_commission_and_trade_count():
    nav = _nav_series([1_000_000, 1_050_000])
    trades = [
        {'trade_date': '2026-01-02', 'ticker': '512400.SH', 'action': 'BUY', 'shares': 100, 'price': 10.0, 'commission': 5.0},
        {'trade_date': '2026-01-02', 'ticker': '515230.SH', 'action': 'BUY', 'shares': 100, 'price': 10.0, 'commission': 5.0},
    ]
    metrics = calculate_account_metrics(nav, trades)
    assert metrics['trade_count'] == 2
    assert metrics['total_commission'] == 10.0


def test_turnover_is_ratio_of_traded_value_to_avg_nav():
    nav = _nav_series([1_000_000, 1_000_000])
    trades = [
        {'trade_date': '2026-01-02', 'ticker': '512400.SH', 'action': 'BUY', 'shares': 1000, 'price': 100.0, 'commission': 5.0},
    ]
    metrics = calculate_account_metrics(nav, trades)
    # Traded value 100,000 / avg NAV 1,000,000 = 10%
    assert metrics['turnover'] == pytest.approx(0.10, abs=0.001)


def test_closed_trade_win_rate():
    trades = [
        {'trade_date': '2026-01-02', 'ticker': '512400.SH', 'action': 'BUY', 'shares': 100, 'price': 10.0, 'commission': 5.0},
        {'trade_date': '2026-01-03', 'ticker': '512400.SH', 'action': 'SELL', 'shares': 100, 'price': 12.0, 'commission': 5.0},
        {'trade_date': '2026-01-04', 'ticker': '515230.SH', 'action': 'BUY', 'shares': 100, 'price': 10.0, 'commission': 5.0},
        {'trade_date': '2026-01-05', 'ticker': '515230.SH', 'action': 'SELL', 'shares': 100, 'price': 9.0, 'commission': 5.0},
    ]
    win_rate = calculate_closed_trade_win_rate(trades)
    assert win_rate == pytest.approx(0.50, abs=0.001)


def test_monthly_win_rate():
    nav = [
        {'nav_date': '2026-01-31', 'nav': 1_000_000},
        {'nav_date': '2026-02-28', 'nav': 1_100_000},
        {'nav_date': '2026-03-31', 'nav': 1_050_000},
        {'nav_date': '2026-04-30', 'nav': 1_200_000},
    ]
    metrics = calculate_account_metrics(nav, [])
    # 3 monthly changes: +10%, -4.5%, +14.3% => 2/3 positive
    assert metrics['monthly_win_rate'] == pytest.approx(2 / 3, abs=0.01)


def test_empty_and_one_day_histories():
    assert calculate_account_metrics([], [])['total_return'] == 0.0
    metrics = calculate_account_metrics([{'nav_date': '2026-01-01', 'nav': 1_000_000}], [])
    assert metrics['total_return'] == 0.0
    assert pd.isna(metrics['sharpe']) or metrics['sharpe'] == 0.0


def test_maximum_drawdown_on_first_day_decline():
    # First day drop from 100 to 90 should produce -10% drawdown.
    nav = _nav_series([100, 90])
    metrics = calculate_account_metrics(nav, [])
    assert metrics['max_drawdown'] == pytest.approx(-0.10, abs=0.001)


def test_b0_4_reference_in_comparison():
    metrics = {
        'B0.4': {'total_return': 0.20, 'annualized_return': 0.20, 'sharpe': 0.90, 'max_drawdown': -0.15, 'calmar': 1.33},
        'A': {'total_return': 0.25, 'annualized_return': 0.25, 'sharpe': 0.95, 'max_drawdown': -0.14, 'calmar': 1.79},
    }
    comparison = build_account_comparison(metrics, reference_name='B0.4')
    assert 'B0.4' in comparison.index
    assert comparison.loc['A', 'total_return_diff'] == pytest.approx(0.05, abs=0.001)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
