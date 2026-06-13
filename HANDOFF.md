# ETF Rotation Model - Context Summary

## Date
2026-06-13

## Branch
`feature/v1.1-defense-rules`

## Status
v1.1 工程上基本跑通。CLI 主流程可用，UI 已修复缩进错误。宽基模块作为实验开关默认关闭。

---

## Completed in This Session

### 1. Wide-Base ETF Fallback Module (IMPLEMENTED, DISABLED BY DEFAULT)
- **Files modified**: `src/config.py`, `src/strategy.py`, `src/backtest.py`, `src/data_fetcher.py`, `main.py`, `app.py`
- **What was added**:
  - `FALLBACK_EQUITY_UNIVERSE` (4 ETFs): 510300.SH, 510500.SH, 159915.SZ, 588000.SH
  - `ALL_TRADABLE_ETFS` (22 total): 16 sector + 4 wide-base + 2 defense
  - `calculate_fallback_equity_score()` in strategy.py: simplified trend-only scoring (no cross-sectional momentum)
  - `fallback_equity_enabled` in config.py: **default False** (validated as negative contribution)
  - 3-tier backtest logic: sector ETF → wide-base fallback → defense fill
  - `bull_market` condition in generate_signals(): close > MA50 and MA50_slope > 0 (for filtering wide-base entry)
- **Why disabled**: Backtest shows wide-base fallback is a **negative contributor** under current trigger rules
  - Fallback OFF: +104.76% total, -14.41% max drawdown, Sharpe 0.78
  - Fallback ON: +93.08% total, -18.48% max drawdown, Sharpe 0.59
  - 107 wide-base buys + 94 sells = excessive turnover, widens drawdown
- **Future path**: Keep as experimental switch. Re-enable only when:
  - Better "strong market" filter (currently 82.3% of rebalance days trigger)
  - Tighter entry thresholds for wide-base (not just prev_close > MA20 * 0.98)
  - Longer holding period for wide-base (avoid frequent churn)

### 2. CLI Main Flow Consistency (FIXED)
- `main.py signal`: Now uses **3-tier scoring pipeline** identical to backtest
  - Step 1-2: Sector ETFs scored individually
  - Step 3: Cross-sectional momentum ranking on sector ETFs only
  - Step 4: Wide-base ETFs scored independently (no ranking)
  - Step 5: Defense assets scored independently (no ranking)
  - Step 6: Merge all scores, generate signals with type-specific thresholds

### 3. Data Fetcher Auto-Update (FIXED)
- `data_fetcher.py fetch_all_data()`: Now downloads all 22 tickers
  - `ETF_CODES` (16 sector) + `FALLBACK_EQUITY_CODES` (4 wide-base) + `DEFENSE_CODES` (2 defense)

### 4. UI Fixes (FIXED)
- `app.py` line 1007: Fixed indentation error where `_get_ticker_name()` was nested inside `if not trades.empty:` block
- `app.py` "重新计算评分" button: Now uses 3-tier pipeline identical to backtest
- `app.py` signal display: Grouped by asset type (sector / wide-base / defense)

### 5. Total Holdings Cap (FIXED)
- `total_max_holdings` set to 5 (same as `max_holdings` for backward compatibility)
- Prevents wide-base + defense from inflating position count beyond 5

---

## Current Code State

### config.py
- `ETF_UNIVERSE`: 16 sector ETFs (rotation pool)
- `FALLBACK_EQUITY_UNIVERSE`: 4 wide-base ETFs (experimental, disabled by default)
- `DEFENSE_UNIVERSE`: 518880.SH (gold), 511010.SH (treasury)
- `ALL_TRADABLE_ETFS`: 22 total (merged dict for data loading)
- `DEFENSE_ALLOCATION`: {0.0: 0.80, 0.2: 0.50, 0.5: 0.20, 1.0: 0.00}
- `DEFENSE_ALLOCATION_MODE`: 'linear'
- `market_timing`: False (default - better backtest performance)
- `fallback_equity_enabled`: False (default - validated as negative contributor)
- `BACKTEST_CONFIG`: initial_capital=1_000_000

### strategy.py
- `calculate_indicators_and_scores()`: Full scoring for sector ETFs (cross-sectional momentum needed)
- `calculate_fallback_equity_score()`: Simplified scoring for wide-base (no cross-sectional momentum)
- `calculate_defense_score()`: Simplified scoring for defense (no cross-sectional momentum)
- `generate_signals()`: 3-tier entry conditions
  - Sector: total_score >= 40, prev_close > ma20, ma20_slope > 0
  - Wide-base: total_score >= 25, prev_close > ma20 * 0.98, ma20_slope > -0.01, **bull_market=True**
  - Defense: total_score >= 30, prev_close > ma20 * 0.98, ma20_slope > -0.001
- `market_timing()`: 2-state (1.0/0.5) using ma50 only, default OFF

### backtest.py
- `run()`: 3-tier scoring pipeline (sector → wide-base → defense)
- `_execute_backtest()`: 3-tier buy logic
  1. Defense config (if market_signal low and defense_enabled)
  2. Sector ETF buy (up to 5 positions)
  3. Wide-base fallback (only if fallback_equity_enabled=True)
  4. Defense fill (if still have slots and defense_enabled)
- Trade execution: uses OPEN price for buys/sells, CLOSE price for NAV
- `initial_capital`: read from `self.cfg.get('initial_capital', ...)`
- DEBUG print: disabled (`if False:` at line 472)

### data_fetcher.py
- `fetch_all_data()`: Downloads all 22 tickers (sector + wide-base + defense + benchmark)

### main.py
- `cmd_backtest()`: Loads all 22 tickers, runs backtest
- `cmd_signal()`: Uses 3-tier scoring pipeline, displays signals by asset type

### app.py
- Sidebar: defense module controls, initial capital input
- "重新计算评分" button: 3-tier pipeline
- Backtest results: defense asset parameter display
- All py_compile checks: PASS

---

## Data Status

| 指标 | 数值 |
|------|------|
| 数据库路径 | database/etf_model.db |
| 行情数据 | ~29,000 条 |
| 标的数量 | 22只 (16 sector + 4 wide-base + 2 defense) |
| 最早日期 | 2019-06-03 |
| 最新日期 | 2026-06-12 |
| 数据源 | iFinD (前复权) + AKShare补充 |

---

## Latest Backtest Results (Default Config)

**Configuration**: market_timing=False, fallback_equity_enabled=False, defense_enabled=True, rebalance_weekday=4, open_price execution

| 指标 | 数值 |
|------|------|
| 总收益 | **104.76%** |
| 年化收益 | **10.53%** |
| 夏普比率 | **0.78** |
| 索提诺比率 | **0.96** |
| 最大回撤 | **-14.41%** |
| 交易次数 | 678 |
| 胜率 | 42.6% |
| 平均盈利 | 6.95% |
| 平均亏损 | -2.93% |
| 总佣金 | 43,392元 |
| 止损次数 | 14 |
| 平均持仓 | 3.3只 |
| 最大持仓 | 5只 |

**Trade breakdown**:
- Sector ETF BUY: 269
- Defense fill BUY: 71
- Wide-base fallback BUY: 0 (disabled)
- Defense config BUY: 0 (market_timing=False → no forced allocation)

---

## Known Issues

| 问题 | 严重程度 | 状态 | 备注 |
|------|---------|------|------|
| 结构性牛市跑输（2019-2021） | 高 | 已知 | 入场阈值太严格，计划v1.2加入板块数据解决 |
| 宽基模块负贡献 | 中 | 已知 | 触发条件太宽，已默认关闭，留作实验开关 |
| 主力资金数据缺失 | 高 | 待解决 | iFinD无直接接口 |
| 无基本面过滤 | 中 | 待接入 | ROE、营收增速 |

---

## Version Definition

- **v1.1 = "Defense + Trading Rules Version"** (COMPLETE)
  - Sector ETF rotation (16 ETFs) + cross-sectional momentum ranking
  - Defense assets (gold/bonds) as low-correlation fill when sector signals insufficient
  - Wide-base ETFs as **experimental fallback** (default disabled)
  - Rebalance day rules (weekly Friday)
  - Dynamic stop-loss (trailing stop, tiered)
  - Cooling period (5 days post-stop-loss)
  - Market timing (simplified 2-state, default OFF)
  - Initial capital adjustable (10K ~ 100M)
- **v1.2 = "Sector Signal Enhancement"** (PENDING)
  - Sector index data integration
  - Sector momentum boost
  - ETF-to-sector mapping
  - Better bull/bear market definition (multi-MA, volatility regime, multi-timeframe)

---

## Next Steps

1. **Git commit**: `feature/v1.1-defense-rules` branch ready for commit
2. **v1.2 planning**: Sector data research (iFinD sector indices, SW industry codes)
3. **Wide-base re-enable**: Only after better "strong market" filter and tighter thresholds

---

## User's Explicit Decisions
- v1.1 mainline: **sector ETF + gold/bond defense fill** (2-tier)
- Wide-base: **experimental module, default off**, keep in code for future testing
- All data-backed changes must be verified by backtest
- Any strategic change must be verified by backtest
