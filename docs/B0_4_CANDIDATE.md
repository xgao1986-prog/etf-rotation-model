# B0.4 候选基线锁定

> **生成时间**：2026-06-21  
> **当前 Git SHA**：`884f529719f5a1e3bb4b9f043675c85ca3286f10`（待更新）  
> **当前分支**：`feature/v1.2.1-regime-adaptive`  
> **数据截止**：2026-06-18  
> **状态**：🟡 候选基线 — 经 B0 数据准入检查通过，待最终确认

---

## 1. 基线声明

**B0.4 是 B0.3 的继任基线候选**。B0.3 因尾部数据缺失（2026-06-08~12 部分 THS 数据）已被标记为**已废止**，B0.4 使用**完整数据**（含补齐的 06-08~12 数据）重新运行。

### 与 B0.3 的差异来源

| 维度 | B0.3（已废止） | B0.4（候选） |
|------|---------------|-------------|
| 数据完整性 | 06-08~12 部分缺失 | 完整（THS + iFinD + AKShare） |
| 数据来源 | iFinD + AKShare | iFinD + AKShare + THS 补齐 |
| 最终 NAV | 2,809,091.21 | 2,761,288.07 |
| 总收益 | 180.91% | 176.13% |
| 交易次数 | 801 | 804 |

**差异解释**：补齐数据后，06-08~12 期间的市场数据（特别是 512480.SH 等 ETF 的低开）被纳入回测，导致部分持仓在关键日期的止损/调仓行为发生变化，最终 NAV 下降约 1.7%。这是**数据更完整后的真实结果**，不是策略错误。

---

## 2. 配置（与 B0.3 完全一致）

B0.4 的**策略参数和交易规则与 B0.3 完全相同**，仅数据更完整：

| 参数 | 值 |
|------|-----|
| `momentum_factor_enabled` | False |
| `volatility_factor_enabled` | False |
| `min_total_score` | 40 |
| `stop_loss` | -8% |
| `stop_loss_mode` | fixed |
| `max_position_per_etf` | 20% |
| `stock_max_holdings` | 5 |
| `use_v2_rebalance` | True |
| `rebalance_weekday` | 3（周四） |
| `market_timing` | False |
| `defense_enabled` | True |
| `fallback_equity_enabled` | False |
| `commission_rate` | 0.03% |
| `min_commission` | 5.0 |

---

## 3. 可交易池：18 只 ETF（与 B0.3 相同）

### 3.1 行业 ETF（16 只）

与 B0.3 完全一致，参见 `docs/B0_BASELINE_LOCK.md` 第 2 节。

### 3.2 防御 ETF（2 只）

与 B0.3 完全一致：518880.SH（黄金）、511010.SH（国债）。

---

## 4. 核心指标（B0.4 候选）

| 指标 | B0.4 值 | B0.3 值（已废止） | 差异 |
|------|---------|------------------|------|
| 最终 NAV | 2,761,288.07 | 2,809,091.21 | -47,803.14 (-1.70%) |
| 总收益 | 176.13% | 180.91% | -4.78% |
| 年化收益 | 16.68% | 16.99% | -0.31% |
| 夏普比率 | 0.8816 | 0.8985 | -0.0169 |
| 最大回撤 | -17.75% | -17.75% | 0% |
| 交易次数 | 804 | 801 | +3 |
| 买入次数 | 399 | 398 | +1 |
| 卖出次数 | 405 | 403 | +2 |
| 调仓次数 | 337 | 337 | 0 |

---

## 5. 数据准入检查

B0.4 候选基线已通过 **B0 数据准入检查 v1**（`scripts/b0_data_admission_check_v1.py`）：

| 检查项 | 状态 | 说明 |
|--------|------|------|
| 完整性检查 | ✅ PASS | 19/19 标的完整 |
| 拼接连续性 | ✅ PASS | 无断档、无重复 |
| 异常跳变检测 | ✅ PASS | 无 OHLC 错误、无极端涨跌幅 |
| 全期抽样 | ✅ PASS | 19/19 通过 |

**准入检查报告**：`docs/B0_DATA_ADMISSION_CHECK_v1.md`  
**数据快照**：`data/snapshots/B0_4_candidate_data_20260621_203250.csv`  
**元数据**：`data/snapshots/B0_4_candidate_metadata_20260621_203250.json`  
**回测指标**：`data/snapshots/B0_4_candidate_metrics_20260621_203453.json`

---

## 6. 自动化测试

B0.4 候选基线通过 4 项自动化测试（`tests/test_b0_data_admission.py`）：

| 测试 | 描述 | 状态 |
|------|------|------|
| test_missing_data_detection | 缺失数据检测 | ✅ PASS |
| test_pre_listing_handling | 上市前数据处理 | ✅ PASS |
| test_complete_data_backtest | 完整数据回测 | ✅ PASS |
| test_admission_check_pass | 准入检查通过 | ✅ PASS |

---

## 7. 确认流程

B0.4 从候选基线转为正式基线需要：

1. ✅ 数据准入检查通过
2. ✅ 完整数据回测完成
3. ✅ 自动化测试通过
4. ⬜ 人工确认差异可接受（NAV 下降 1.7% 来自数据完整性，非策略退化）
5. ⬜ 更新 `docs/B0_BASELINE_LOCK.md` 为正式 B0.4 锁定
6. ⬜ 标记 B0.3 为 "已废止，不可复现"

---

## 8. 可复现运行命令

```python
from config import build_config, ETF_UNIVERSE, DEFENSE_UNIVERSE, BENCHMARK
from database import ETFDatabase
from backtest import BacktestEngine

cfg = build_config()
cfg['fallback_equity_enabled'] = False
cfg['momentum_factor_enabled'] = False
cfg['volatility_factor_enabled'] = False

db = ETFDatabase()
tickers = sorted(set(list(ETF_UNIVERSE.keys()) + list(DEFENSE_UNIVERSE.keys())))
market_df = db.get_market_data(ticker=tickers, start_date='2019-01-01', end_date='2026-06-18')
bench_df = db.get_market_data(ticker=BENCHMARK, start_date='2019-01-01', end_date='2026-06-18')

engine = BacktestEngine(cfg)
result = engine.run(market_df, bench_df, as_of_date='2026-06-18')
```

---

*本文档为 B0.4 候选基线，待确认后转为正式基线。变更历史参见 `docs/CHANGES.md`。*
