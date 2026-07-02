# Paper Trading UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a standalone “虚拟盘” tab to the existing Streamlit app so users can create accounts from saved presets, run comparison accounts, confirm shadow-account orders, and compare performance.

**Architecture:** Keep `app.py` as a thin page router. Put reusable virtual-account workflows in `src/paper_trading/ui_service.py`, performance calculations in `src/paper_trading/metrics.py`, and Streamlit rendering in `src/paper_trading/ui.py`. Extend the existing store and service only where durable account, order, NAV, and manual-fill behavior is required.

**Tech Stack:** Python, Streamlit, SQLite, pandas, pytest, existing `src/paper_trading` package.

---

## File Map

- Create `src/strategy_presets.py`: shared preset loading and validation without importing Streamlit.
- Create `src/paper_trading/ui_service.py`: batch creation, summaries, multi-account runs, shadow-order actions.
- Create `src/paper_trading/metrics.py`: KPI and account comparison calculations.
- Create `src/paper_trading/ui.py`: Streamlit rendering only.
- Modify `src/paper_trading/models.py`: order statuses and validated manual-fill input.
- Modify `src/paper_trading/store.py`: durable order lifecycle, NAV history, account detail queries.
- Modify `src/paper_trading/service.py`: validated public operations used by the UI.
- Modify `src/paper_trading/runner.py`: comparison accounts auto-fill; shadow accounts remain pending.
- Modify `app.py`: add one top-level “虚拟盘” tab.
- Add focused tests for each new module and update existing paper-trading tests.
- Update `docs/CHANGES.md` and `docs/CURRENT_STATE.md` only after verification.

## Task 1: Shared Saved-Preset Reader

**Files:**
- Create `src/strategy_presets.py`
- Modify `app.py`
- Create `tests/test_strategy_presets.py`

- [ ] Write tests proving a valid JSON preset loads unchanged.
- [ ] Write tests rejecting missing weights, non-positive `max_holdings`, invalid `max_position_per_etf`, and non-negative stop loss.
- [ ] Run `py -m pytest tests/test_strategy_presets.py -v` and confirm the new tests fail.
- [ ] Add `DEFAULT_PRESET_PATH`, `load_strategy_presets`, `save_strategy_presets`, and `validate_strategy_preset`.
- [ ] Change the current preset page in `app.py` to use the shared functions without changing its behavior or JSON format.
- [ ] Run `py -m pytest tests/test_strategy_presets.py tests/test_preset_loading.py -v`.
- [ ] Commit only the preset reader, its tests, and the required `app.py` change with message `refactor: share saved strategy presets`.

## Task 2: Durable Shadow-Order Lifecycle

**Files:**
- Modify `src/paper_trading/models.py`
- Modify `src/paper_trading/store.py`
- Modify `src/paper_trading/service.py`
- Modify `tests/test_paper_trading_store.py`
- Modify `tests/test_paper_trading_service.py`

- [ ] Add failing tests for `FILLED`, `REJECTED`, `CANCELLED`, and `EXPIRED`.
- [ ] Add failing tests proving unconfirmed orders do not change cash or positions.
- [ ] Add failing tests for insufficient cash, oversell, non-100-share quantity, wrong account, and repeated confirmation.
- [ ] Run the store and service tests and confirm the new cases fail.
- [ ] Add `OrderStatus` values: `PENDING`, `FILLED`, `SKIPPED`, `CANCELLED`, `REJECTED`, `EXPIRED`.
- [ ] Add immutable `ManualFill(account_id, order_id, trade_date, actual_price, actual_shares)` validation.
- [ ] Add store methods:
  - `list_orders(account_id, status=None, start_date=None, end_date=None)`
  - `get_order(order_id)`
  - `update_order_status(order_id, status, reason=None)`
  - `expire_pending_orders(account_id, before_trade_date)`
  - `apply_manual_fill(fill_row, trade_row, position_rows, nav_row)`
  - `list_nav_history(account_id, start_date=None, end_date=None)`
- [ ] Make `apply_manual_fill` update order, trade, position, cash, and NAV in one transaction.
- [ ] Add service methods:
  - `confirm_shadow_order(fill)`
  - `reject_shadow_order(account_id, order_id, reason)`
  - `cancel_shadow_order(account_id, order_id, reason)`
  - `expire_shadow_orders(account_id, trade_date)`
- [ ] Require a shadow account and a currently `PENDING` order for every manual action.
- [ ] Run `py -m pytest tests/test_paper_trading_store.py tests/test_paper_trading_service.py -v`.
- [ ] Commit with message `feat: add shadow order confirmation lifecycle`.

## Task 3: Account Creation and Batch Operations

**Files:**
- Create `src/paper_trading/ui_service.py`
- Create `tests/test_paper_trading_ui_service.py`

- [ ] Add failing tests for batch creation from two saved presets with one shared initial capital and start date.
- [ ] Add failing tests for manually entered positive capital and empty preset selection.
- [ ] Add failing tests for duplicate account prevention.
- [ ] Add failing tests for an imported shadow account whose cash plus position value equals opening NAV.
- [ ] Add failing tests proving an account’s configuration hash does not change when the source preset is later edited.
- [ ] Add failing tests proving one failed account does not prevent other selected accounts from running.
- [ ] Implement `PaperTradingUIService` with:
  - `create_comparison_accounts`
  - `create_shadow_account`
  - `list_account_summaries`
  - `run_accounts`
  - `list_pending_shadow_orders`
- [ ] Give accounts created in one comparison batch a shared `group_id`.
- [ ] Generate deterministic account IDs from account type, preset name, start date, and a short configuration hash.
- [ ] Return per-account success and failure records from `run_accounts`.
- [ ] Run `py -m pytest tests/test_paper_trading_ui_service.py -v`.
- [ ] Commit with message `feat: add paper trading UI workflows`.

## Task 4: Comparison and Shadow Execution Rules

**Files:**
- Modify `src/paper_trading/runner.py`
- Modify `tests/test_paper_trading_runner.py`

- [ ] Add an end-to-end test proving a comparison account executes a due signal automatically.
- [ ] Add an end-to-end test proving a shadow account produces `PENDING` orders and no trades.
- [ ] Assert the shadow account’s cash and positions remain unchanged before confirmation.
- [ ] Add a test proving a previous trading day’s pending order becomes `EXPIRED`.
- [ ] Add a test proving comparison-account orders are not expired by the shadow-order rule.
- [ ] Run `py -m pytest tests/test_paper_trading_runner.py -v` and confirm the new cases fail.
- [ ] Refactor signal handling so order construction is shared, while execution depends on account type.
- [ ] Keep the existing comparison-account automatic path unchanged.
- [ ] Expire old pending shadow orders before processing the current trading day.
- [ ] Run `py -m pytest tests/test_paper_trading_runner.py -v`.
- [ ] Commit with message `feat: separate comparison and shadow execution`.

## Task 5: Performance Metrics

**Files:**
- Create `src/paper_trading/metrics.py`
- Create `tests/test_paper_trading_metrics.py`

- [ ] Add deterministic tests using a known NAV series for cumulative return and maximum drawdown.
- [ ] Add tests for annualized return, Sharpe, Calmar, commission, trade count, and turnover.
- [ ] Define completed-trade win rate using only closed buy/sell round trips.
- [ ] Define monthly win rate using month-end NAV changes.
- [ ] Add tests for empty and one-day histories.
- [ ] Add tests proving B0.4 is used as the named comparison reference when present.
- [ ] Run `py -m pytest tests/test_paper_trading_metrics.py -v` and confirm failure.
- [ ] Implement:
  - `calculate_account_metrics`
  - `calculate_closed_trade_win_rate`
  - `build_account_comparison`
- [ ] Run `py -m pytest tests/test_paper_trading_metrics.py -v`.
- [ ] Commit with message `feat: add paper trading performance metrics`.

## Task 6: Streamlit Virtual-Account Page

**Files:**
- Create `src/paper_trading/ui.py`
- Create `tests/test_paper_trading_ui.py`
- Modify `app.py`

- [ ] Add tests using a mocked `PaperTradingUIService`; do not duplicate business logic in UI tests.
- [ ] Test that account creation passes the manually entered capital unchanged.
- [ ] Test that only saved preset names are selectable and no strategy parameter editor appears.
- [ ] Test that selected-account execution calls the service once.
- [ ] Test that shadow fills require an explicit confirmation control.
- [ ] Test that rejection requires a reason.
- [ ] Run `py -m pytest tests/test_paper_trading_ui.py -v` and confirm failure.
- [ ] Implement `render_paper_trading_page(ui_service, data_provider)`.
- [ ] Render five sections:
  1. 账户总览
  2. 创建账户
  3. 今日运行
  4. 待确认订单
  5. 账户详情与策略对比
- [ ] Use forms for creation and order decisions so a Streamlit rerun cannot submit twice.
- [ ] Add “虚拟盘” as a seventh top-level tab in `app.py`.
- [ ] Initialize the existing database at `database/paper_trading.db`.
- [ ] Pass market data and scores through an adapter; do not query them directly inside individual widgets.
- [ ] Run UI, preset, live-assistant, and daily-workflow tests.
- [ ] Commit with message `feat: add virtual account dashboard`.

## Task 7: Browser and Financial Acceptance

**Files:**
- Modify only when a verified defect is found.

- [ ] Run `py -m py_compile` for `app.py` and every new or changed Python module.
- [ ] Run all paper, UI, preset, live, and rebalance tests.
- [ ] Run `py -m pytest tests/test_app_b0_signature.py tests/test_b0_data_admission.py tests/test_b0_4_slippage.py -q`.
- [ ] Confirm 30 frozen-baseline tests pass and NAV remains 2,761,288.07.
- [ ] Start the application with `py -m streamlit run app.py`.
- [ ] Open the page in a browser and create at least two comparison accounts from saved presets.
- [ ] Confirm both accounts use the same manually entered initial capital and date.
- [ ] Create one shadow account from cash and imported positions.
- [ ] Run all three accounts for a representative trading day.
- [ ] Confirm comparison accounts auto-fill and the shadow account remains unchanged.
- [ ] Confirm one shadow order with edited price and quantity.
- [ ] Reject one shadow order with a reason.
- [ ] Cancel one shadow order.
- [ ] Advance to the next trading day and confirm an untouched order expires.
- [ ] Refresh and rerun the same date; confirm no duplicate order or trade appears.
- [ ] Verify for each account:
  - cash plus position market value equals NAV
  - BUY cash decrease equals amount plus commission
  - SELL cash increase equals amount minus commission
  - every position quantity is a multiple of 100
- [ ] Recheck all existing top-level pages for rendering and interaction regressions.

## Task 8: Documentation and Handoff

**Files:**
- Modify `docs/CHANGES.md`
- Modify `docs/CURRENT_STATE.md`

- [ ] Record only the functionality actually delivered.
- [ ] Record exact test commands and real pass counts.
- [ ] Record the browser flows actually exercised.
- [ ] Record financial reconciliation results and remaining limitations.
- [ ] Run `git diff --check`.
- [ ] Confirm `git diff --name-only 6baa91f..HEAD` contains only files required by this feature plus the approved design and plan.
- [ ] Do not include historical untracked reports, presets, research scripts, or temporary files.
- [ ] Commit documentation with message `docs: close paper trading UI phase`.
- [ ] Push the feature branch.
- [ ] Report Base SHA, Target SHA, branch, local/remote equality, file list, test outputs, browser checks, and known limitations.
- [ ] Do not claim completion if the browser workflow was not actually opened and exercised.
