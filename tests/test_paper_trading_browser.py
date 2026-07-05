#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Browser acceptance tests for the Streamlit virtual-account page.

Requires playwright. Run with:
    pytest tests/test_paper_trading_browser.py -v -s
"""

import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

BROWSER_URL = os.environ.get('STREAMLIT_URL', 'http://127.0.0.1:8501')
MAIN_DB_PATH = os.environ.get(
    'MAIN_DB_PATH',
    os.path.join(os.path.dirname(__file__), '..', 'database', 'etf_model.db'),
)
PRESET_NAME = 'v1.0 原始参数'


def _main_conn():
    return sqlite3.connect(MAIN_DB_PATH)


def _get_latest_market_date():
    conn = _main_conn()
    try:
        cur = conn.execute("SELECT MAX(date) FROM market_data")
        row = cur.fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        raise RuntimeError(f"no market data in {MAIN_DB_PATH}")
    return str(row[0])


def _test_db_path():
    return os.environ['PAPER_TRADING_TEST_DB_PATH']


def _query_test_db(query, params=()):
    conn = sqlite3.connect(_test_db_path())
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(query, params)
        rows = cur.fetchall()
    finally:
        conn.close()
    return rows


def _take_screenshot(page, name):
    screenshots_dir = os.path.join(
        os.path.dirname(__file__), '..', 'reports', 'browser_acceptance'
    )
    os.makedirs(screenshots_dir, exist_ok=True)
    path = os.path.join(screenshots_dir, f'{name}.png')
    page.screenshot(path=path, full_page=True)
    return path


def _active_top_panel_id(page):
    """Return the id of the currently active top-level tab panel."""
    return page.locator('[role="tab"][aria-selected="true"]:visible').first.get_attribute(
        'aria-controls'
    )


def _open_virtual_account_tab(page):
    """Click the outer '虚拟盘' tab (last of the visible outer tabs)."""
    tabs = page.locator('[role="tab"]:visible')
    last_tab = tabs.last
    last_tab.click()
    # Wait until the virtual-account tab is selected and its sub-tabs render.
    for _ in range(60):
        if last_tab.get_attribute('aria-selected') != 'true':
            time.sleep(1)
            continue
        panel_id = _active_top_panel_id(page)
        if page.locator(f'#{panel_id} [role="tab"]').count() > 0:
            break
        time.sleep(1)
    time.sleep(2)


def _open_inner_tab(page, index):
    """Click a tab inside the virtual-account page by zero-based index.

    Indices: 0 账户总览, 1 创建账户, 2 今日运行,
             3 待确认订单, 4 账户详情与策略对比.
    """
    panel_id = _active_top_panel_id(page)
    tabs = page.locator(f'#{panel_id} [role="tab"]')
    if tabs.count() <= index:
        raise RuntimeError(
            f'inner tab {index} not found in panel {panel_id} (only {tabs.count()} tabs)'
        )
    tabs.nth(index).click()
    time.sleep(3)


def _click_first_multiselect_option(page, panel_id):
    """Open the first multiselect in the panel and select its first option."""
    multiselect = page.locator(f'#{panel_id} [data-testid="stMultiSelect"]').first
    multiselect.locator('input').first.click()
    time.sleep(2)
    options = page.locator('[data-baseweb="popover"] [role="option"]').all()
    assert len(options) > 0, 'no multiselect options available'
    options[0].click()
    time.sleep(1)
    # Close the dropdown so the form submit button is reachable.
    page.keyboard.press('Escape')
    time.sleep(1)


def _select_account_type(page, panel_id, index):
    """Switch the '账户类型' selectbox to the option at *index*."""
    selectbox = page.locator(f'#{panel_id} [data-testid="stSelectbox"] input').first
    selectbox.click()
    time.sleep(2)
    options = page.locator('[data-baseweb="popover"] [role="option"]').all()
    assert len(options) > index, f'account type option {index} not found'
    options[index].click()
    time.sleep(5)


def _fill_shadow_account_name(page, panel_id, name):
    """Fill the shadow account name text input."""
    text_inputs = page.locator(f'#{panel_id} input[type="text"]').all()
    assert len(text_inputs) > 0, 'shadow account name input not found'
    text_inputs[0].fill(name)
    time.sleep(1)


def _click_first_form_submit(page, panel_id):
    """Click the first form-submit button inside the active panel."""
    btn = page.locator(f'#{panel_id} [data-testid="stBaseButton-secondaryFormSubmit"]').first
    btn.click()
    time.sleep(10)


def _click_run_button(page, panel_id):
    """Click the '运行选中账户' button inside the active panel."""
    btn = page.locator(f'#{panel_id} [data-testid="stBaseButton-secondary"]').first
    btn.click()
    time.sleep(15)


def _click_first_visible_order_action(page, panel_id):
    """Click the first visible action button in the pending-orders tab."""
    buttons = page.locator(f'#{panel_id} [data-testid="stBaseButton-secondaryFormSubmit"]').all()
    visible = [b for b in buttons if b.is_visible()]
    assert len(visible) > 0, 'no visible order action buttons'
    visible[0].click()
    time.sleep(10)


@pytest.fixture(scope='module')
def streamlit_server():
    """Start Streamlit with a temporary paper-trading database."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, 'paper_trading_test.db')
        os.environ['PAPER_TRADING_TEST_DB_PATH'] = db_path
        env = os.environ.copy()
        env['PAPER_TRADING_DB_PATH'] = db_path

        proc = subprocess.Popen(
            [
                'python',
                '-m',
                'streamlit',
                'run',
                'app.py',
                '--server.address=127.0.0.1',
                '--server.port=8501',
                '--server.headless=true',
                '--browser.gatherUsageStats=false',
            ],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        def _drain_stdout():
            try:
                for _line in proc.stdout:
                    pass
            except Exception:
                pass

        threading.Thread(target=_drain_stdout, daemon=True).start()

        for _ in range(60):
            try:
                import urllib.request

                urllib.request.urlopen(BROWSER_URL, timeout=1)
                break
            except Exception:
                time.sleep(1)
        else:
            proc.terminate()
            try:
                out, _ = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                out = '<could not retrieve stdout>'
            raise RuntimeError(f'Streamlit server failed to start:\n{out}')

        yield BROWSER_URL

        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@pytest.fixture(scope='module')
def browser_page(streamlit_server):
    pytest.importorskip('playwright')
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 1400, 'height': 900})
        page = context.new_page()
        yield page
        context.close()
        browser.close()


class TestStreamlitVirtualAccount:
    def test_virtual_account_full_workflow(self, browser_page):
        page = browser_page
        data_date = _get_latest_market_date()

        page.goto(BROWSER_URL, wait_until='networkidle', timeout=120000)
        page.wait_for_selector('[role="tab"]:visible', timeout=60000)
        time.sleep(5)

        # 1. Create a comparison account through the UI
        _open_virtual_account_tab(page)
        _open_inner_tab(page, 1)
        panel_id = _active_top_panel_id(page)
        _click_first_multiselect_option(page, panel_id)
        _click_first_form_submit(page, panel_id)

        accounts = _query_test_db(
            "SELECT * FROM paper_accounts WHERE account_type='COMPARISON'"
        )
        assert len(accounts) >= 1, 'comparison account was not created'
        _take_screenshot(page, '01_comparison_account_created')

        # 2. Create a shadow account through the UI
        _open_virtual_account_tab(page)
        _open_inner_tab(page, 1)
        panel_id = _active_top_panel_id(page)
        _select_account_type(page, panel_id, 1)  # switch to 影子账户
        # Rerun resets the outer tab; navigate back to the create-account tab.
        _open_virtual_account_tab(page)
        _open_inner_tab(page, 1)
        panel_id = _active_top_panel_id(page)
        _fill_shadow_account_name(page, panel_id, '测试影子')
        _click_first_form_submit(page, panel_id)

        shadow_accounts = _query_test_db(
            "SELECT * FROM paper_accounts WHERE account_type='SHADOW'"
        )
        assert len(shadow_accounts) >= 1, 'shadow account was not created'
        shadow_id = shadow_accounts[0]['account_id']
        _take_screenshot(page, '02_shadow_account_created')

        # 3. Run selected accounts through the UI
        _open_virtual_account_tab(page)
        _open_inner_tab(page, 2)
        panel_id = _active_top_panel_id(page)
        _click_run_button(page, panel_id)

        nav_records = _query_test_db(
            "SELECT * FROM paper_daily_nav WHERE nav_date=?", (data_date,)
        )
        assert len(nav_records) >= 2, 'expected NAV records for both accounts'
        pending_orders = _query_test_db(
            "SELECT * FROM paper_orders WHERE account_id=? AND status='PENDING'",
            (shadow_id,),
        )
        assert len(pending_orders) >= 1, 'no pending order generated for shadow account'
        _take_screenshot(page, '03_run_completed')

        # 4. Confirm the pending shadow order through the UI
        _open_virtual_account_tab(page)
        _open_inner_tab(page, 3)
        panel_id = _active_top_panel_id(page)
        _click_first_visible_order_action(page, panel_id)

        trades = _query_test_db(
            "SELECT * FROM paper_trades WHERE account_id=? AND ticker=?",
            (shadow_id, pending_orders[0]['ticker']),
        )
        assert len(trades) >= 1, 'trade was not recorded after confirmation'

        filled_orders = _query_test_db(
            "SELECT * FROM paper_orders WHERE account_id=? AND status='FILLED'",
            (shadow_id,),
        )
        assert len(filled_orders) >= 1, 'order was not marked FILLED after confirmation'
        _take_screenshot(page, '04_order_confirmed')

        # 5. Account details and strategy comparison render
        _open_virtual_account_tab(page)
        _open_inner_tab(page, 4)
        time.sleep(2)
        _take_screenshot(page, '05_account_details')
