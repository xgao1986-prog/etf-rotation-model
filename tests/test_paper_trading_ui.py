#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_paper_trading_ui.py — Streamlit virtual-account page tests."""

import os
import sys
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from paper_trading.ui import render_paper_trading_page
from strategy_presets import load_strategy_presets


def _first_preset_name():
    presets = load_strategy_presets()
    return next(iter(presets)) if presets else 'B0.4'


def _make_ui_service():
    service = MagicMock()
    service.list_account_summaries.return_value = []
    service.list_pending_shadow_orders.return_value = []
    service.create_comparison_accounts.return_value = ['acct-1']
    service.create_shadow_account.return_value = 'shadow-1'
    service.run_accounts.return_value = {'success': {}, 'failure': {}}
    service.service.store.list_nav_history.return_value = []
    service.service.store.list_trades.return_value = []
    service.service.store.list_orders.return_value = []
    service.service.store.list_positions.return_value = []
    return service


def _coverage(
    expected=18,
    actual_prices=18,
    missing_prices=None,
    invalid_prices=None,
    actual_scores=22,
    missing_scores=None,
    invalid_scores=None,
):
    return {
        'expected_price_count': expected,
        'actual_price_count': actual_prices,
        'missing_prices': missing_prices or [],
        'invalid_prices': invalid_prices or [],
        'actual_score_count': actual_scores,
        'missing_scores': missing_scores or [],
        'invalid_scores': invalid_scores or [],
    }


def _data_provider(data_date='2026-06-29', **coverage_overrides):
    def provider():
        return {}, {}, pd.DataFrame({'ticker': [], 'total_score': []}), data_date, _coverage(**coverage_overrides)
    return provider


def _make_mock_st(config=None):
    """Build a mocked streamlit module for direct-function UI tests."""
    config = config or {}
    mock_st = MagicMock()

    # Tabs returns five tab mocks that can be used as context managers.
    mock_st.tabs.return_value = [MagicMock() for _ in range(5)]

    # Columns returns a list of column mocks that can be used as context managers.
    def _columns_side_effect(n=1, *args, **kwargs):
        return [MagicMock() for _ in range(n)]
    mock_st.columns.side_effect = _columns_side_effect

    # Inputs
    mock_st.selectbox.return_value = config.get('selectbox', '对比账户')
    mock_st.multiselect.return_value = config.get('multiselect', [])
    mock_st.number_input.return_value = config.get('number_input', 1_000_000.0)
    mock_st.date_input.return_value = config.get('date_input', pd.Timestamp('2026-06-29'))
    mock_st.text_input.return_value = config.get('text_input', '')
    mock_st.text_area.return_value = config.get('text_area', '')
    # selectbox is also used for account-type and account-detail selection;
    # callers that need a specific value must pass it explicitly.
    mock_st.button.return_value = config.get('button', False)

    submit_config = config.get('form_submit_button', {})
    if isinstance(submit_config, dict):
        def _submit_by_label(label='', *args, **kwargs):
            return submit_config.get(label, False)
        mock_st.form_submit_button.side_effect = _submit_by_label
    else:
        mock_st.form_submit_button.return_value = submit_config

    return mock_st


def test_page_renders_without_error():
    ui_service = _make_ui_service()
    mock_st = _make_mock_st()
    with patch('paper_trading.ui.st', mock_st):
        render_paper_trading_page(ui_service, _data_provider)


def test_create_account_passes_capital_unchanged():
    ui_service = _make_ui_service()
    mock_st = _make_mock_st({
        'selectbox': '对比账户',
        'multiselect': [_first_preset_name()],
        'number_input': 1_500_000.0,
        'date_input': pd.Timestamp('2026-06-30'),
        'form_submit_button': {'批量创建': True},
    })
    with patch('paper_trading.ui.st', mock_st):
        render_paper_trading_page(ui_service, _data_provider)

    ui_service.create_comparison_accounts.assert_called_once()
    _, kwargs = ui_service.create_comparison_accounts.call_args
    assert kwargs['initial_capital'] == 1_500_000.0


def test_no_strategy_parameter_editor_in_page():
    ui_service = _make_ui_service()
    mock_st = _make_mock_st()
    with patch('paper_trading.ui.st', mock_st):
        render_paper_trading_page(ui_service, _data_provider)
    assert mock_st.slider.call_count == 0


def _summary(account_id, name, **overrides):
    base = {
        'account_id': account_id,
        'name': name,
        'account_type': 'COMPARISON',
        'strategy_name': name.split()[0],
        'cash': 1_000_000.0,
        'positions_value': 0.0,
        'nav': 1_000_000.0,
        'initial_capital': 1_000_000.0,
        'latest_nav_date': '2026-06-29',
        'status': 'ACTIVE',
        'group_id': 'grp-1',
        'start_date': '2026-06-29',
    }
    base.update(overrides)
    return base


def test_run_selected_accounts_calls_service_once_per_account():
    ui_service = _make_ui_service()
    ui_service.list_account_summaries.return_value = [
        _summary('a1', 'A1', strategy_name='A'),
    ]
    mock_st = _make_mock_st({
        'button': True,
        'selectbox': 'A1 (a1)',
    })
    with patch('paper_trading.ui.st', mock_st):
        render_paper_trading_page(ui_service, _data_provider('2026-06-29'))

    ui_service.run_accounts.assert_called_once()


def test_run_results_preserved_and_displayed_after_rerun():
    """Results stored in session_state must remain visible after st.rerun()."""
    ui_service = _make_ui_service()
    ui_service.list_account_summaries.return_value = [
        _summary('a1', 'A1', strategy_name='A'),
    ]
    mock_st = _make_mock_st({
        'button': False,  # simulate post-rerun render without a new click
        'multiselect': ['a1'],
        'selectbox': 'A1 (a1)',
    })
    mock_st.session_state = {
        'paper_trading_run_results': {
            'success': {'a1': 'ok'},
            'failure': {},
        }
    }
    with patch('paper_trading.ui.st', mock_st):
        render_paper_trading_page(ui_service, _data_provider('2026-06-29'))

    mock_st.success.assert_called_once_with('成功运行 1 个账户')


def test_run_blocked_when_trade_date_mismatches_data_date():
    ui_service = _make_ui_service()
    ui_service.list_account_summaries.return_value = [
        _summary('a1', 'A1', strategy_name='A'),
    ]
    mock_st = _make_mock_st({
        'button': True,
        'selectbox': 'A1 (a1)',
        'date_input': pd.Timestamp('2026-07-03'),
    })
    with patch('paper_trading.ui.st', mock_st):
        render_paper_trading_page(ui_service, _data_provider('2026-06-29'))

    ui_service.run_accounts.assert_not_called()
    mock_st.error.assert_called()
    error_message = mock_st.error.call_args[0][0]
    assert '2026-06-29' in error_message
    assert '2026-07-03' in error_message


def test_run_blocked_when_data_date_is_unknown():
    ui_service = _make_ui_service()
    ui_service.list_account_summaries.return_value = [
        _summary('a1', 'A1', strategy_name='A'),
    ]
    mock_st = _make_mock_st({
        'button': True,
        'selectbox': 'A1 (a1)',
    })
    with patch('paper_trading.ui.st', mock_st):
        render_paper_trading_page(ui_service, _data_provider(data_date=None))

    ui_service.run_accounts.assert_not_called()
    mock_st.error.assert_called()


def test_shadow_fill_requires_confirmation_control():
    ui_service = _make_ui_service()
    ui_service.list_pending_shadow_orders.return_value = [
        {
            'order_id': 'o1',
            'account_id': 's1',
            'ticker': '512400.SH',
            'action': 'BUY',
            'delta_shares': 100,
            'reference_price': 1.0,
            'status': 'PENDING',
            'trade_date': '2026-07-03',
        }
    ]
    mock_st = _make_mock_st()  # all submit buttons default to False
    with patch('paper_trading.ui.st', mock_st):
        render_paper_trading_page(ui_service, _data_provider)

    ui_service.service.confirm_shadow_order.assert_not_called()


def test_confirm_shadow_order_calls_service_when_submitted():
    ui_service = _make_ui_service()
    ui_service.list_pending_shadow_orders.return_value = [
        {
            'order_id': 'o1',
            'account_id': 's1',
            'ticker': '512400.SH',
            'action': 'BUY',
            'delta_shares': 100,
            'reference_price': 1.0,
            'status': 'PENDING',
            'trade_date': '2026-07-03',
        }
    ]
    mock_st = _make_mock_st({
        'form_submit_button': {'✅ 确认成交': True},
    })
    with patch('paper_trading.ui.st', mock_st):
        render_paper_trading_page(ui_service, _data_provider)

    ui_service.service.confirm_shadow_order.assert_called_once()


def test_reject_requires_reason():
    ui_service = _make_ui_service()
    ui_service.list_pending_shadow_orders.return_value = [
        {
            'order_id': 'o1',
            'account_id': 's1',
            'ticker': '512400.SH',
            'action': 'BUY',
            'delta_shares': 100,
            'reference_price': 1.0,
            'status': 'PENDING',
            'trade_date': '2026-07-03',
        }
    ]
    mock_st = _make_mock_st({
        'text_input': '',
        'form_submit_button': {'❌ 标记未执行': True},
    })
    with patch('paper_trading.ui.st', mock_st):
        render_paper_trading_page(ui_service, _data_provider)

    ui_service.service.reject_shadow_order.assert_not_called()


def test_reject_shadow_order_calls_service_when_reason_provided():
    ui_service = _make_ui_service()
    ui_service.list_pending_shadow_orders.return_value = [
        {
            'order_id': 'o1',
            'account_id': 's1',
            'ticker': '512400.SH',
            'action': 'BUY',
            'delta_shares': 100,
            'reference_price': 1.0,
            'status': 'PENDING',
            'trade_date': '2026-07-03',
        }
    ]
    mock_st = _make_mock_st({
        'text_input': '未成交',
        'form_submit_button': {'❌ 标记未执行': True},
    })
    with patch('paper_trading.ui.st', mock_st):
        render_paper_trading_page(ui_service, _data_provider)

    ui_service.service.reject_shadow_order.assert_called_once_with('s1', 'o1', '未成交')


def test_cancel_shadow_order_calls_service_when_reason_provided():
    ui_service = _make_ui_service()
    ui_service.list_pending_shadow_orders.return_value = [
        {
            'order_id': 'o1',
            'account_id': 's1',
            'ticker': '512400.SH',
            'action': 'BUY',
            'delta_shares': 100,
            'reference_price': 1.0,
            'status': 'PENDING',
            'trade_date': '2026-07-03',
        }
    ]
    mock_st = _make_mock_st({
        'text_input': '撤单',
        'form_submit_button': {'🚫 取消': True},
    })
    with patch('paper_trading.ui.st', mock_st):
        render_paper_trading_page(ui_service, _data_provider)

    ui_service.service.cancel_shadow_order.assert_called_once_with('s1', 'o1', '撤单')


def test_comparison_uses_same_group_and_start_date_and_finds_b0_4_reference():
    ui_service = _make_ui_service()
    ui_service.list_account_summaries.return_value = [
        _summary('b0', 'B0.4 2026-06-29', strategy_name='B0.4'),
        _summary('a1', 'A 2026-06-29', strategy_name='A'),
        _summary('o1', 'Other batch', strategy_name='A', group_id='grp-2'),
    ]
    ui_service.service.store.list_nav_history.return_value = [
        {'nav_date': '2026-06-29', 'nav': 1_000_000.0}
    ]
    mock_st = _make_mock_st({'selectbox': 'A 2026-06-29 (a1)'})

    with patch('paper_trading.ui.st', mock_st), \
         patch('paper_trading.ui.calculate_account_metrics', return_value={
             'total_return': 0.1,
             'annualized_return': 0.25,
             'sharpe': 0.9,
             'max_drawdown': -0.05,
             'calmar': 5.0,
             'total_commission': 100.0,
             'trade_count': 5,
             'turnover': 0.5,
             'win_rate': 0.6,
             'monthly_win_rate': 0.5,
         }) as mock_metrics, \
         patch('paper_trading.ui.build_account_comparison', return_value=pd.DataFrame({'x': [1]})) as mock_compare:
        render_paper_trading_page(ui_service, _data_provider())

    mock_compare.assert_called_once()
    args, kwargs = mock_compare.call_args
    accounts_metrics = args[0]
    assert kwargs['reference_name'] == 'B0.4 2026-06-29'
    assert 'B0.4 2026-06-29' in accounts_metrics
    assert 'A 2026-06-29' in accounts_metrics
    assert 'Other batch' not in accounts_metrics


def test_run_blocked_when_price_coverage_incomplete():
    ui_service = _make_ui_service()
    ui_service.list_account_summaries.return_value = [
        _summary('a1', 'A1', strategy_name='A'),
    ]
    mock_st = _make_mock_st({
        'button': True,
        'selectbox': 'A1 (a1)',
    })
    provider = _data_provider(
        '2026-06-29',
        actual_prices=17,
        missing_prices=['518880.SH'],
    )
    with patch('paper_trading.ui.st', mock_st):
        render_paper_trading_page(ui_service, provider)

    ui_service.run_accounts.assert_not_called()
    mock_st.error.assert_called()
    error_message = ' '.join(call[0][0] for call in mock_st.error.call_args_list if call[0])
    assert '518880.SH' in error_message


def test_run_blocked_when_scores_empty():
    ui_service = _make_ui_service()
    ui_service.list_account_summaries.return_value = [
        _summary('a1', 'A1', strategy_name='A'),
    ]
    mock_st = _make_mock_st({
        'button': True,
        'selectbox': 'A1 (a1)',
    })
    provider = _data_provider('2026-06-29', actual_scores=0)
    with patch('paper_trading.ui.st', mock_st):
        render_paper_trading_page(ui_service, provider)

    ui_service.run_accounts.assert_not_called()
    mock_st.error.assert_called()


def test_run_blocked_when_price_is_nan():
    ui_service = _make_ui_service()
    ui_service.list_account_summaries.return_value = [
        _summary('a1', 'A1', strategy_name='A'),
    ]
    mock_st = _make_mock_st({
        'button': True,
        'selectbox': 'A1 (a1)',
    })
    provider = _data_provider(
        '2026-06-29',
        invalid_prices=[{'ticker': '518880.SH', 'reason': '收盘价无效 (nan)'}],
    )
    with patch('paper_trading.ui.st', mock_st):
        render_paper_trading_page(ui_service, provider)

    ui_service.run_accounts.assert_not_called()
    mock_st.error.assert_called()
    error_message = ' '.join(call[0][0] for call in mock_st.error.call_args_list if call[0])
    assert '518880.SH' in error_message
    assert 'nan' in error_message


def test_run_blocked_when_price_is_zero():
    ui_service = _make_ui_service()
    ui_service.list_account_summaries.return_value = [
        _summary('a1', 'A1', strategy_name='A'),
    ]
    mock_st = _make_mock_st({
        'button': True,
        'selectbox': 'A1 (a1)',
    })
    provider = _data_provider(
        '2026-06-29',
        invalid_prices=[{'ticker': '518880.SH', 'reason': '收盘价无效 (0.0)'}],
    )
    with patch('paper_trading.ui.st', mock_st):
        render_paper_trading_page(ui_service, provider)

    ui_service.run_accounts.assert_not_called()
    mock_st.error.assert_called()


def test_run_blocked_when_price_is_negative():
    ui_service = _make_ui_service()
    ui_service.list_account_summaries.return_value = [
        _summary('a1', 'A1', strategy_name='A'),
    ]
    mock_st = _make_mock_st({
        'button': True,
        'selectbox': 'A1 (a1)',
    })
    provider = _data_provider(
        '2026-06-29',
        invalid_prices=[{'ticker': '518880.SH', 'reason': '收盘价无效 (-1.0)'}],
    )
    with patch('paper_trading.ui.st', mock_st):
        render_paper_trading_page(ui_service, provider)

    ui_service.run_accounts.assert_not_called()
    mock_st.error.assert_called()


def test_run_blocked_when_total_score_is_nan():
    ui_service = _make_ui_service()
    ui_service.list_account_summaries.return_value = [
        _summary('a1', 'A1', strategy_name='A'),
    ]
    mock_st = _make_mock_st({
        'button': True,
        'selectbox': 'A1 (a1)',
    })
    provider = _data_provider(
        '2026-06-29',
        invalid_scores=[{'ticker': '518880.SH', 'reason': 'total_score 无效 (nan)'}],
    )
    with patch('paper_trading.ui.st', mock_st):
        render_paper_trading_page(ui_service, provider)

    ui_service.run_accounts.assert_not_called()
    mock_st.error.assert_called()
    error_message = ' '.join(call[0][0] for call in mock_st.error.call_args_list if call[0])
    assert '518880.SH' in error_message
    assert 'total_score' in error_message


def test_overview_includes_performance_metrics():
    ui_service = _make_ui_service()
    ui_service.list_account_summaries.return_value = [
        _summary('a1', 'A1', strategy_name='A'),
    ]
    ui_service.service.store.list_nav_history.return_value = [
        {'nav_date': '2026-06-29', 'nav': 1_000_000.0},
        {'nav_date': '2026-06-30', 'nav': 1_100_000.0},
    ]
    mock_st = _make_mock_st({'selectbox': 'A1 (a1)'})
    with patch('paper_trading.ui.st', mock_st):
        render_paper_trading_page(ui_service, _data_provider())

    assert ui_service.service.store.list_nav_history.called
    df_call = mock_st.dataframe.call_args
    rendered_df = df_call[0][0]
    assert '年化收益' in rendered_df.columns
    assert '夏普' in rendered_df.columns
    assert '最大回撤' in rendered_df.columns
    assert 'Calmar' in rendered_df.columns
    assert '胜率' in rendered_df.columns
    assert '换手' in rendered_df.columns
    assert '佣金' in rendered_df.columns


def test_details_shows_orders_and_nav_curves():
    ui_service = _make_ui_service()
    ui_service.list_account_summaries.return_value = [
        _summary('a1', 'A1', strategy_name='A'),
    ]
    ui_service.service.store.list_nav_history.return_value = [
        {'nav_date': '2026-06-29', 'nav': 1_000_000.0},
        {'nav_date': '2026-06-30', 'nav': 1_100_000.0},
    ]
    ui_service.service.store.list_orders.return_value = [
        {'order_id': 'o1', 'ticker': '512400.SH', 'action': 'BUY', 'delta_shares': 100}
    ]
    mock_st = _make_mock_st({'selectbox': 'A1 (a1)'})
    with patch('paper_trading.ui.st', mock_st):
        render_paper_trading_page(ui_service, _data_provider())

    assert ui_service.service.store.list_orders.called
    assert mock_st.plotly_chart.call_count >= 2


def test_comparison_excludes_shadow_accounts_without_group_id():
    ui_service = _make_ui_service()
    ui_service.list_account_summaries.return_value = [
        _summary('b0', 'B0.4 2026-06-29', strategy_name='B0.4'),
        _summary('a1', 'A 2026-06-29', strategy_name='A'),
        _summary('s1', 'Shadow 1', account_type='SHADOW', group_id=None),
        _summary('s2', 'Shadow 2', account_type='SHADOW', group_id=None),
    ]
    ui_service.service.store.list_nav_history.return_value = [
        {'nav_date': '2026-06-29', 'nav': 1_000_000.0}
    ]
    mock_st = _make_mock_st({'selectbox': 'A 2026-06-29 (a1)'})

    with patch('paper_trading.ui.st', mock_st), \
         patch('paper_trading.ui.calculate_account_metrics', return_value={
             'total_return': 0.1,
             'annualized_return': 0.25,
             'sharpe': 0.9,
             'max_drawdown': -0.05,
             'calmar': 5.0,
             'total_commission': 100.0,
             'trade_count': 5,
             'turnover': 0.5,
             'win_rate': 0.6,
             'monthly_win_rate': 0.5,
         }), \
         patch('paper_trading.ui.build_account_comparison', return_value=pd.DataFrame({'x': [1]})) as mock_compare:
        render_paper_trading_page(ui_service, _data_provider())

    mock_compare.assert_called_once()
    args, kwargs = mock_compare.call_args
    accounts_metrics = args[0]
    assert 'B0.4 2026-06-29' in accounts_metrics
    assert 'A 2026-06-29' in accounts_metrics
    assert 'Shadow 1' not in accounts_metrics
    assert 'Shadow 2' not in accounts_metrics


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
