# ETF Rotation Model - Context Summary (v1.1 In-Progress)

## Date
2026-06-12

## Branch
`feature/v1.1-experimental` (uncommitted changes on top of last commit)

## Uncommitted Changes (Git status)
- `HANDOFF.md` - modified (this file)
- `PROGRESS.md` - modified
- `app.py` - modified (defense UI added)
- `reports/defense_module_report.md` - modified (Known Limitations added)
- `src/backtest.py` - modified (defense module v1.3 + separation logic)
- `src/config.py` - modified (gold/bonds removed from ETF_UNIVERSE, DEFENSE_UNIVERSE added, market_timing=False)
- `src/strategy.py` - modified (calculate_defense_score + defense_mask in generate_signals, market_timing simplified to 2-state)
- Untracked: `check_look_ahead.py`, `reports/defense_fix_test.txt`, `reports/final_v1.1_test.txt`, `reports/separation_test.txt`, `scripts/diagnose_bull_period.py`, `scripts/diagnose_defense_scores.py`, `scripts/test_defense_fix.py`, `scripts/test_defense_signals.py`, `scripts/test_defense_toggle.py`, `test_timing_compare.py`, `test_two_step_rebalance.py`

## Critical Issues (Unresolved at End of Last Session)

### 1. Defense Asset Separation Partially Implemented (UNTESTED)
- **Gold/bonds removed from ETF_UNIVERSE** (line 39-56) - they now only exist in DEFENSE_UNIVERSE (line 66-69)
- **backtest.py** now separates stock_df and defense_df before scoring (lines 47-53)
- **stock ETFs** go through full scoring pipeline (calculate_scores → rank_all_momentum → compute_total_score)
- **defense assets** go through simplified scoring (calculate_defense_score) and are merged after total_score is computed
- **NaN total_score fix**: lines 91-95 in backtest.py try to fill NaN total_score for defense assets using component columns
- **PROBLEM**: The defense asset scoring logic in `calculate_defense_score` does not include `volume_score` and `vol_score` in the defense_cols list for total_score calculation. The defense_cols are: `['trend_score', 'confirm_score', 'momentum_rank', 'volume_score', 'vol_score']` but `calculate_defense_score` sets `volume_score=5` and `vol_score=10`. So total_score should be correct after the fillna.
- **UNTESTED**: The last test failed because `data_cache/cleaned_sample/sample_market.csv` doesn't exist. The test script tried to use PythonRun but the path had issues (backslashes not escaped, `python` not found).

### 2. Market Timing Changed to 2-State (NEEDS REVIEW)
- `strategy.py` line 227-251: `market_timing()` now only produces 1.0 (close > MA50) or 0.5 (close <= MA50)
- **Original was 3-state**: 1.0 (close > MA20), 0.5 (MA20 >= close > MA50), 0.2 (close <= MA50)
- **This change was made during implementation** without explicit user approval for this specific simplification
- The user had discussed wanting 4-state or more nuanced bull/bear definition, but this 2-state simplification was done by the assistant
- `config.py` line 102: `market_timing: False` - **DEFENSE MODULE IS DISABLED BY DEFAULT** because market timing is off. When False, generate_signals sets market_signal=1.0 for all dates, so defense_allocation is always 0.0.
- **This needs to be reconciled**: if defense module is the core v1.1 feature, market_timing should be True. But the assistant set it to False with comment "回测数据显示关闭后收益更高".

### 3. Bull Market Underperformance (DIAGNOSED, NOT FIXED)
- **User complaint**: In 2019-2021 bull market, strategy only gained ~7% while CSI300 gained 36%
- **Root cause identified by assistant** (from `scripts/diagnose_bull_period.py`):
  1. **Exit too sensitive**: All 104 sells in bull period were "跌破20日均线调出". Normal bull market corrections cause temporary MA20 breaches, triggering full liquidation.
  2. **Entry lag**: Requires 4+ days of confirm_score accumulation. By the time an ETF qualifies, the uptrend move is already partially over.
  3. **Sector rotation failure**: In 2019-2021 structural bull (core assets), strategy rotated into lagging sectors (bank, military, media) while missing the leaders (consumer, pharma, tech).
- **User decision**: Wait for v1.2 (sector data enhancement) to fix sector selection. The entry/exit sensitivity issue is a pure trading rules issue that could be fixed in v1.1.
- **Proposed fixes (not implemented)**:
  - A: Lower entry thresholds when market_signal=1.0 (bull market)
  - B: Add 3-day buffer before selling on MA20 breach
  - C: Allow 5% buffer on MA20 when in bull market

### 4. Defense Asset Not Configuring Enough (FIXED, BUT UNTESTED)
- **Original issue**: Defense assets weren't being bought in sufficient quantities
- **Fixes applied**:
  1. **Forced reduction**: When market_signal < 1.0 and current_positions_value > target_total_value, sell lowest-scoring non-defense positions first
  2. **Loop through all defense assets**: Instead of only buying defense_signals.iloc[0], loop through all defense tickers
  3. **Defense priority**: Defense assets get priority in position allocation
- **Defense toggle fix**: Added `if _defense_allocation > 0 and self.cfg.get('defense_enabled', True):` check so UI can disable defense module
- **Test result from before separation** (pre-separation): Gold-only: 50.92% total, 0.676 Sharpe, -10.38% drawdown; Dual-defense: 46.06%, 0.620 Sharpe, -10.02% drawdown
- **Post-separation tests**: FAILED - no data available to run tests

### 5. Position Not Full in Bull Market (DIAGNOSED, NOT FIXED)
- **User complaint**: Even with 25% per-ETF cap, positions are often not full and sometimes empty
- **Root cause**: Equal weight + per-ETF cap = hard ceiling. 5 positions × 15% = 75% max. With 3 qualifying ETFs, only 45% invested. Plus strict entry conditions cause empty positions.
- **Assistant proposed**: Use up all cash when market_signal=1.0 (allow temporary over-allocation)
- **Not implemented**

### 6. App.py UI Issues (FIXED, PARTIALLY)
- `build_config` call updated from `experimental_cfg=` to `build_config(strategy_cfg=..., trading_rules_cfg=..., defense_cfg=...)`
- Defense module UI added in sidebar (enable checkbox, mode selection, allocation sliders)
- Defense asset parameter display added to backtest results
- **BUT**: `app.py` imports `ETF_UNIVERSE` from config. If gold/bonds are removed from ETF_UNIVERSE, the UI might not display them in the ETF pool table. Need to check if app.py also needs to import DEFENSE_UNIVERSE for display purposes.

## Current Code State

### config.py
- `ETF_UNIVERSE`: 16 stock ETFs only (gold/bonds removed)
- `DEFENSE_UNIVERSE`: 518880.SH (gold), 511010.SH (treasury)
- `DEFENSE_ALLOCATION`: {0.0: 0.80, 0.2: 0.50, 0.5: 0.20, 1.0: 0.00}
- `DEFENSE_ALLOCATION_MODE`: 'linear'
- `market_timing`: False (PROBLEM - disables defense module)
- `STRATEGY_CONFIG` has 20+ experimental keys for v1.1 features

### backtest.py
- `run()` method: separates stock_df and defense_df, runs separate scoring pipelines
- `calculate_defense_score` called for defense assets (from strategy.py)
- `total_score` fillna logic after merging defense assets (lines 91-95)
- Defense module with forced reduction, loop-through-all-defense-assets, and priority allocation
- DEBUG print still active at line 472 (`if True:`)

### strategy.py
- `market_timing()`: 2-state (1.0/0.5) - simplified from 3-state
- `calculate_defense_score()`: Simplified scoring for defense assets (no cross-sectional momentum)
- `generate_signals()`: Has defense_mask (lines 291-297) with relaxed thresholds for defense assets
- `get_latest_signals()`: Only uses stock ETF thresholds, does NOT use defense_mask

## Test Files Generated (Untracked)
- `scripts/test_defense_fix.py` - tests defense module fixes (pre-separation)
- `scripts/test_defense_toggle.py` - tests defense enabled/disabled toggle
- `scripts/test_defense_signals.py` - tests defense asset signal generation (post-separation)
- `scripts/diagnose_bull_period.py` - diagnoses bull market underperformance
- `scripts/diagnose_defense_scores.py` - diagnoses defense asset scoring issues
- `reports/defense_compariso

- `reports/dynamic_defense_comparison.csv` - dynamic allocation comparison
- `reports/separation_test.txt` - post-separation test output (possibly empty or failed)

## Next Steps (From User's Last Request)
The user's last request was to:
1. **Fix the defense asset separation** (gold/bonds not in rotation, only in defense) - PARTIALLY DONE, UNTESTED
2. **Research and improve bull/bear market definition** - DIAGNOSED, NOT IMPLEMENTED. User wants better methods from research (GitHub, forums, skills). Specifically:
   - Multi-MA methods (dual/triple/MACD+MA/RSRS)
   - Volatility regime switching
   - Multi-timeframe confirmation
   - Position management: inverse volatility sizing, Kelly, fixed risk, dynamic based on market state
3. **Fix bull market position not full/empty** - DIAGNOSED, NOT IMPLEMENTED
4. **Fix entry/exit sensitivity** - DIAGNOSED, NOT IMPLEMENTED (user wants to wait for v1.2 sector data, but assistant suggested doing it in v1.1)

## User's Explicit Decisions
- Defense assets (gold/bonds) should **only** be used in bear market for capital utilization, not part of daily rotation
- Wait for v1.2 (sector data) to fix the structural bull market sector rotation issue
- Any strategic change must be data-backed (verified by backtest)
- The "data-driven discovery" section should be maintained in HANDOFF.md

## Known Risks
- `market_timing=False` means defense module is disabled by default. The UI checkbox might enable it but the underlying config has it off.
- `DEFENSE_ALLOCATION` has key 0.0 but `market_timing` only produces 0.5 as the lowest signal. If market_timing is ever set to produce 0.0, the defense allocation would be 80%.
- The `data_cache` directory and `sample_market.csv` don't exist. Any test that tries to load them will fail. Need to use `database.get_historical_data()` or similar DB access method instead.
- The `calculate_defense_score` does not compute `volume_ratio` properly for defense assets (set to 1.0), but `total_score` fillna uses the component sum which should work.
- `get_latest_signals()` in strategy.py does NOT apply the defense_mask. If the app uses this for real-time signals, defense assets won't show up even when they should.
