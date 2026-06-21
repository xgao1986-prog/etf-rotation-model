# B0.4 正式基线锁定

> **锁定时间**：2026-06-21  
> **当前 Git SHA**：`ea07e9202b0cdbaa2f68614e89ada65bc790c210`  
> **当前分支**：`feature/v1.2.1-regime-adaptive`  
> **数据截止**：2026-06-18  
> **锁定状态**：✅ 已锁定 — 任何变更须经独立 A/B 测试并正式更新本文件

---

## 1. 唯一基准声明

本项目当前唯一用于策略评估、实验对照和版本演进的主线是：

> **B0 18 只 ETF 基准版 → B0.4 冻结基线**

- **B0** 表示当前 18 只 ETF 基准策略家族。
- **B0.4** 是该家族当前冻结的正式比较基线，取代已废止的 B0.3。
- B0.4 与 B0.3 策略参数完全相同，仅数据更完整（补齐 06-08~12 尾部数据）。
- 后续所有研究、诊断和功能实验，都必须以 B0.4 为 A/B 测试的对照组。
- 未经过独立验证并正式更新本文件的研究结果，不得取代 B0.4 成为新基线。

### B0.3 已废止（历史记录）

> ⚠️ **B0.3 状态：已废止（OBSOLETE）** — 2026-06-21
> 
> B0.3 因尾部数据缺失（2026-06-08~12 部分 THS 数据未完整纳入），无法在当前数据库上复现原始冻结指标（NAV=2,809,091）。
> 补齐数据后运行结果：NAV=2,761,288（差异 -1.7%），数据完整性导致，非策略退化。
> B0.3 不可复现，历史冻结指标仅供参考，不得作为新实验的对照组。

---

## 2. 可交易池：18 只 ETF（固定）

### 2.1 行业 ETF（16 只）

| 代码 | 名称 | 行业分组 |
|------|------|----------|
| 512480.SH | 半导体ETF | chip |
| 515230.SH | 软件ETF | software |
| 515880.SH | 通信ETF | telecom |
| 512010.SH | 医药ETF | medicine |
| 159928.SZ | 消费ETF | consumption |
| 516160.SH | 新能源ETF | new_energy |
| 516110.SH | 汽车ETF | auto |
| 512800.SH | 银行ETF | finance |
| 512000.SH | 券商ETF | finance |
| 512660.SH | 军工ETF | military |
| 512980.SH | 传媒ETF | tech_media |
| 512400.SH | 有色金属ETF | metal |
| 159996.SZ | 家电ETF | appliance |
| 159865.SZ | 养殖ETF | livestock |
| 159697.SZ | 油气ETF | energy |
| 159530.SZ | 机器人ETF | robot |

### 2.2 防御 ETF（2 只）

| 代码 | 名称 | 用途 |
|------|------|------|
| 518880.SH | 黄金ETF | 低相关防御/现金替代 |
| 511010.SH | 国债ETF | 低相关防御/现金替代 |

### 2.3 基准指数

- `000300.SH`（沪深 300）——仅用于基准比较和市场观察，不属于 18 只可交易 ETF。

---

## 3. 评分因子配置

与 B0.3 完全一致，未做任何修改。

### 3.1 关闭的因子

| 因子 | 开关 | 状态 |
|------|------|------|
| 动量排名 (momentum_rank) | `momentum_factor_enabled = False` | ❌ 关闭 |
| 波动率 (vol_score) | `volatility_factor_enabled = False` | ❌ 关闭 |

### 3.2 生效的因子

| 因子 | 权重 | 说明 |
|------|------|------|
| 趋势强度 (trend_score) | 30% | 收盘价>MA20(+15) + >MA50(+10) + MA20斜率>0(+5) |
| 趋势确认 (confirm_score) | 20% | 连续在MA20之上的天数×4，最多5天(20分) |
| 成交量 (volume_score) | 15% | 放量上涨(+15) / 放量(+10) / 普通(+5) |
| **合计** | **65%** | 动量25%和波动率10%关闭后，总分理论上限65 |

### 3.3 入场阈值（核心池）

| 参数 | 值 | 说明 |
|------|-----|------|
| `min_trend_score` | 15 | 趋势最低分 |
| `min_confirm_score` | 4 | 确认最低分（至少1天在均线之上） |
| `min_total_score` | 40 | 总评分最低分 |
| 差市场门槛 | 55 | 当所有行业ETF动量中位数<0时，总评分门槛提高到55 |
| `prev_close > ma20` | 是 | 前一日收盘价必须站上20日均线 |
| `ma20_slope > 0` | 是 | 20日均线斜率必须为正 |
| `history_count >= 51` | 是 | 第51个交易日（索引50）才是第一个完整指标集 |

### 3.4 防御资产入场阈值

| 参数 | 值 | 说明 |
|------|-----|------|
| `min_defense_trend_score` | 10 | 比核心池低5分 |
| `min_defense_confirm_score` | 2 | 比核心池低2分 |
| `min_defense_total_score` | 25 | 比核心池低15分 |
| `prev_close > ma20 * 0.98` | 是 | 允许2%缓冲 |
| `ma20_slope > -0.001` | 是 | 允许均线微跌 |

### 3.5 评分权重（完整配置）

```python
weights = {
    'trend': 0.30,
    'confirm': 0.20,
    'momentum': 0.25,   # 关闭（momentum_factor_enabled=False）
    'volume': 0.15,
    'volatility': 0.10,  # 关闭（volatility_factor_enabled=False）
}
```

---

## 4. 交易规则

与 B0.3 完全一致，未做任何修改。完整四层卖出机制参见 B0.3 历史文档或 `scripts/b0_3_baseline.py`。

### 4.1 买入条件（核心池，必须同时满足）

1. `trend_score >= 15`
2. `confirm_score >= 4`
3. `total_score >= 40`（差市场时>=55）
4. `prev_close > ma20`
5. `ma20_slope > 0`

### 4.2 卖出条件（四层）

- 第一层：固定止损 -8%（每日触发）
- 第二层：跌破趋势条件（调仓日信号生成）
- 第三层：调出BUY候选（调仓日执行）
- 第四层：防御让路（调仓日执行）

### 4.3 关键参数

| 参数 | 值 |
|------|-----|
| `stop_loss_mode` | `fixed` |
| `stop_loss` | -8% |
| `defense_enabled` | True |
| `fallback_equity_enabled` | False（已关闭） |
| `stock_max_holdings` | 5 |
| `total_max_holdings` | 5 |
| `max_position_per_etf` | 20% |
| `use_v2_rebalance` | True |
| `rebalance_weekday` | 3（周四） |
| `market_timing` | False |
| `commission_rate` | 0.03% |
| `min_commission` | 5.0 |

---

## 5. 交易执行口径

与 B0.3 完全一致。

- T 日收盘后信号生成，T+1 开盘执行（`open` 价格）
- 所有指标使用 `shift(1)` 避免未来数据泄露
- 第 51 个交易日（索引 50）才是第一个完整指标集
- 100 股整手，佣金 0.03% 双向，最低 5 元
- 当前不计滑点

---

## 6. 数据区间与样本划分

| 区间 | 起止日期 | 说明 |
|------|----------|------|
| 回测起始 | 2019-06-03 | 策略开始运行日（首个完整指标集约2019-08-13） |
| 回测结束 | 2026-06-18 | 当前数据截止日 |
| 训练期 | 2019-06-03 ~ 2023-12-31 | 参数验证和诊断使用 |
| 验证期 | 2024-01-01 ~ 2024-12-31 | 样本外验证 |
| 封存 OOS | 2025-01-01 ~ 2026-06-18 | **已封存，不得用于参数选择或反向调优** |

> 策略首个完整交易信号约在 2019-08-13（第 51 个完整指标集后），实际回测从该日起开始产生有效持仓。

---

## 7. 长期基准指标（B0.4 已锁定）

以下指标来自**完整数据回测**（2026-06-21），数据含补齐的 06-08~12 尾部数据：

| 指标 | B0.4 值 | B0.3 已废止值 | 差异 | 来源 |
|------|---------|---------------|------|------|
| 最终 NAV | **2,761,288.07** | 2,809,091.21 | -47,803 (-1.70%) | 完整数据回测 |
| 总收益 | **176.13%** | 180.91% | -4.78% | 完整数据回测 |
| 年化收益 | **16.68%** | 16.99% | -0.31% | 完整数据回测 |
| 夏普比率 | **0.8816** | 0.8985 | -0.0169 | 完整数据回测 |
| 最大回撤 | **-17.75%** | -17.75% | 0% | 完整数据回测 |
| 交易次数 | **804** | 801 | +3 | 完整数据回测 |
| 买入次数 | **399** | 398 | +1 | 完整数据回测 |
| 卖出次数 | **405** | 403 | +2 | 完整数据回测 |
| 调仓次数 | **337** | 337 | 0 | 完整数据回测 |
| 初始资金 | 1,000,000 | 1,000,000 | 0 | 配置 |

**差异解释**：补齐 06-08~12 期间的市场数据（特别是 512480.SH 等 ETF 的低开）被纳入回测，导致部分持仓在关键日期的止损/调仓行为发生变化，最终 NAV 下降约 1.7%。这是**数据更完整后的真实结果**，不是策略错误。

### 7.1 封存 OOS 指标（已冻结，来源不变）

| 指标 | OOS 值 | 来源 |
|------|--------|------|
| 年化收益 | 42.16% | `reports/phase5_final_oos.md` |
| 夏普比率 | 1.9086 | `reports/phase5_final_oos.md` |
| 最大回撤 | -11.36% | `reports/phase5_final_oos.md` |
| 数据区间 | 2025-01-01 ~ 2026-06-18 | 已封存 |

> 该 OOS 区间已经封存，**不得继续用于参数选择或反向调优**。

---

## 8. 数据快照与可复现性

### 8.1 数据快照（SHA-256 校验）

| 文件 | 路径 | 说明 |
|------|------|------|
| 数据快照 | `data/snapshots/B0_4_candidate_data_20260621_210815.csv` | 110,236 条市场数据 |
| 元数据 | `data/snapshots/B0_4_candidate_metadata_20260621_210815.json` | 含 SHA-256 校验 |
| 回测指标 | `data/snapshots/B0_4_candidate_metrics_20260621_203453.json` | 核心指标 |

### 8.2 SHA-256 校验值

```yaml
database_file:
  sha256: e0cf29931df02a9ba3df5ca465804ee0ee70f120f800ed01ccad744901b58ef0
dataset_19_tickers:
  sha256: 1ecf8f66f8ac51bb0964971f1e73a46cc13e1e9685f0fda569bd655c9bebd721
```

### 8.3 数据准入检查

B0.4 基线数据已通过 **B0 数据准入检查 v1.1**（`scripts/b0_data_admission_check_v1.py`）：

| 检查项 | 状态 | 说明 |
|--------|------|------|
| 完整性检查 | ✅ PASS | 19/19 标的完整 |
| 拼接连续性 | ✅ PASS | 无断档、无重复 |
| 异常跳变检测 | ✅ PASS | 无 OHLC 错误、无极端涨跌幅 |
| 全期抽样 | ⚠️ WARN | 7只ETF known_coverage 缺失（数据源未覆盖早期），anomalous_internal=0 |

**exit code：1**（WARN：数据可准入，但需知晓 7 只 ETF 早期数据覆盖不足）

### 8.4 自动化测试

B0.4 基线通过 8 项自动化测试（`tests/test_b0_data_admission.py`）：

| 测试 | 描述 | 状态 |
|------|------|------|
| test_missing_data_antipattern | 内存中删除成熟ETF交易日，断言准入失败 | ✅ PASS |
| test_backtest_blocked_on_admission_failure | mock 准入失败，断言 RuntimeError + BacktestEngine.run 未调用 | ✅ PASS |
| test_pre_listing_handling | 策略自动跳过历史不足50天的ETF | ✅ PASS |
| test_complete_data_backtest | 验证 B0.4 指标文件存在 | ✅ PASS |
| test_admission_check_pass | 无异常缺口，有 known_coverage（不全PASS） | ✅ PASS |
| test_authoritative_listing_date | 权威上市日 ≠ 数据库 MIN(date) | ✅ PASS |
| test_historical_gap_classification | 区分 known_coverage / anomalous_internal | ✅ PASS |
| test_snapshot_metadata_hashes | 元数据 SHA-256 为 64 位十六进制 | ✅ PASS |

---

## 9. 代码完整性校验

### 9.1 当前代码 Git SHA

```
ea07e9202b0cdbaa2f68614e89ada65bc790c210
```

### 9.2 关键文件 SHA256

| 文件 | SHA256 | 大小 |
|------|--------|------|
| `src/config.py` | `0f66682261a632064d2d5836bc0a764274f735960ddd4be9ef9dad6967106afd` | 24,869 B |
| `src/strategy.py` | `7f5ba74385303037e5a61f225e9e582a277eabb41e46e4fd22ce1b29b6929a52` | 25,240 B |
| `src/backtest.py` | `8982ed4b81f5682cfd9053399814b57e41f2f43e280de93eb8825972d782edc0` | 106,251 B |
| `src/database.py` | `91f2cea50fd5864a63217b333fbb07db73c7d6f39a21bc95f05a32d2f84465c9` | 22,638 B |

### 9.3 配置哈希（可复现性）

以下关键参数的组合可唯一标识 B0.4 配置：

```python
# B0.4 配置指纹（与 B0.3 完全相同）
momentum_factor_enabled = False
volatility_factor_enabled = False
min_total_score = 40
stop_loss = -0.08
stop_loss_mode = 'fixed'
max_position_per_etf = 0.20
stock_max_holdings = 5
use_v2_rebalance = True
rebalance_weekday = 3
market_timing = False
defense_enabled = True
fallback_equity_enabled = False
commission_rate = 0.0003
min_commission = 5.0
```

---

## 10. 可复现的基准运行命令

### 10.1 生成 B0.4 基准报告

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

### 10.2 关键指标验证 checklist

| 检查项 | 预期值 | 容差 |
|--------|--------|------|
| 最终 NAV | 2,761,288.07 | ±100 |
| 总收益 | 176.13% | ±0.01% |
| 年化收益 | 16.68% | ±0.01% |
| 夏普比率 | 0.8816 | ±0.0001 |
| 最大回撤 | -17.75% | ±0.01% |
| 交易次数 | 804 | 精确匹配 |
| 买入次数 | 399 | 精确匹配 |
| 卖出次数 | 405 | 精确匹配 |
| 调仓次数 | 337 | 精确匹配 |

如果任何指标超出容差，**停止并报告差异，不得直接更新本文件**。

---

## 11. 明确不属于 B0.4 的内容

以下内容存在于代码库中，但**不属于 B0.4 冻结基线**：

| 内容 | 状态 | 说明 |
|------|------|------|
| 32 只 ETF 池（概念 ETF） | ❌ 不属于 B0.4 | 16 只行业 + 16 只概念是研究框架，非生产基线 |
| `fixed_32` 回测结果 | ❌ 不属于 B0.4 | 不同标的池和信息集合，不能代表 B0.4 |
| 申万行业指数数据 (`SECTOR_INDEX_UNIVERSE`) | ❌ 不属于 B0.4 | v1.2/v1.3 研究用，未用于 B0.4 交易决策 |
| 市场状态 `active` 模式 | ❌ 不属于 B0.4 | `MARKET_REGIME_CONFIG['mode'] = 'observer'`，仅观察不改参数 |
| ATR 动态止损 | ❌ 不属于 B0.4 | Phase 6.5 诊断后未采纳，当前仍为固定止损 |
| 凯利仓位优化 | ❌ 不属于 B0.4 | Phase 8.1 诊断后未进入，当前仍为等权 |
| Phase 6~8 研究 | ❌ 不属于 B0.4 | 诊断结果，未修改策略参数 |
| 滑点模拟 | ❌ 不属于 B0.4 | 未来稳健性验证，当前不计滑点 |
| 持仓稳定机制 | ❌ 不属于 B0.4 | `STABILITY_CONFIG['enabled'] = False` |
| 宽基补仓 (`FALLBACK_EQUITY_UNIVERSE`) | ❌ 不属于 B0.4 | `fallback_equity_enabled = False` |
| 动态止盈 | ❌ 不属于 B0.4 | `trailing_stop_mode = 'none'` |
| 冷静期 | ❌ 不属于 B0.4 | `cooling_period = 0` |

---

## 12. 基线更新流程

如需更新 B0.4 基线，必须：

1. **以 B0.4 为对照**开展独立 A/B 测试
2. **每次只修改一个变量**
3. **在训练期和验证期都优于 B0.4**，且风险未恶化
4. **新鲜回测验证**新配置与冻结报告的指标差异
5. **如果差异超出容差，停止并报告**，不得直接更新
6. **正式更新** `docs/B0_BASELINE_LOCK.md`、`docs/CURRENT_VERSION_NOTE.md`、`docs/CHANGES.md`
7. **提交 Git** 并记录变更历史

---

*本文档由 B0.4 正式基线锁定流程生成。变更历史参见 `docs/CHANGES.md`。*
