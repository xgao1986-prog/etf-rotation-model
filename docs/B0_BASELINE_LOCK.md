# B0.3 正式基线锁定

> ⚠️ **状态：已废止（OBSOLETE）** — 2026-06-21
> 
> B0.3 因尾部数据缺失（2026-06-08~12 部分 THS 数据未完整纳入），无法在当前数据库上复现原始冻结指标（NAV=2,809,091）。
> 补齐数据后运行结果：NAV=2,761,288（差异 -1.7%），数据完整性导致，非策略退化。
> 
> **继任基线**：B0.4 候选基线（`docs/B0_4_CANDIDATE.md`）
> 
> ---
> **原锁定时间**：2026-06-21  
> **当前 Git SHA**：`884f529719f5a1e3bb4b9f043675c85ca3286f10`  
> **当前分支**：`feature/v1.2.1-regime-adaptive`  
> **数据截止**：2026-06-18  
> **原锁定状态**：❌ 已废止 — 尾部数据缺失，无法在当前数据库上复现

---

## 1. 唯一基准声明

本项目当前唯一用于策略评估、实验对照和版本演进的主线是：

> **B0 18 只 ETF 基准版 → B0.3 冻结基线**

- **B0** 表示当前 18 只 ETF 基准策略家族。
- **B0.3** 是该家族当前冻结的正式比较基线。
- 后续所有研究、诊断和功能实验，都必须以 B0.3 为 A/B 测试的对照组。
- 未经过独立验证并正式更新本文件的研究结果，不得取代 B0.3 成为新基线。

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

### 4.1 买入条件（核心池，必须同时满足）

1. `trend_score >= 15`
2. `confirm_score >= 4`
3. `total_score >= 40`（差市场时>=55）
4. `prev_close > ma20`
5. `ma20_slope > 0`

### 4.2 卖出条件（调仓日 + 每日触发）

B0.3 卖出机制分四层，按触发时机和优先级如下：

#### 第一层：固定止损（每日触发，最高优先级）

| 参数 | 值 | 说明 |
|------|-----|------|
| `stop_loss_mode` | `fixed` | 固定止损模式 |
| `stop_loss` | -8% | 相对于成本价的亏损阈值 |
| 触发条件 | `current_price < cost * (1 - 0.08)` | 当日开盘价低于成本价92%时触发 |
| 执行价格 | 当日开盘价 | 止损卖出按当日开盘价成交 |
| 例外 | 不检查`history_count` | 已持仓的ETF不受51天成熟限制 |

> 每日开盘时检查所有持仓，如果亏损达到-8%即触发止损。止损在调仓日之前独立执行，不等待调仓日。

#### 第二层：跌破趋势条件（调仓日信号生成）

在 `generate_signals` 中，如果持仓的前一日收盘价跌破20日均线：

```python
sell_mask = scores_df['prev_close'] < scores_df['ma20']
scores_df.loc[sell_mask, 'signal_type'] = 'SELL'
```

- 这意味着该ETF当日的 `signal_type` 被设为 `'SELL'`，不再出现在BUY候选列表中
- 但**不会立即卖出**，而是等待调仓日由调仓引擎统一处理
- 如果该ETF已持仓，在下一个调仓日将被调出候选列表并卖出

#### 第三层：调出BUY候选（调仓日执行）

在 `plan_rebalance_v2_5` 中：

1. 首先分类持仓和候选：
   - 保留的行业持仓：在"可交易"候选列表中的行业持仓
   - 保留的防御持仓：在"可交易"候选列表中的防御持仓

2. 需要卖出的持仓：不在候选列表中的持仓
   ```python
   if t in industry_tickers and t not in tradable_industry_tickers:
       sell_tickers.append(t)  # 行业ETF不在BUY候选 → 卖出
   elif t in defense_tickers and t not in tradable_defense_tickers:
       sell_tickers.append(t)  # 防御ETF不在BUY候选 → 卖出
   ```

3. 卖出原因标注为：`"调出候选列表"`

> 注意：B0.3 中 `rank_buffer_enabled=False`，不存在排名缓冲（跌出Top N才卖）的额外约束。持仓只要不在BUY候选列表就会被卖出。

#### 第四层：防御让路（调仓日执行）

在 `plan_rebalance_v2_5` 中，当行业槽位不足时：

```python
if industry_slots < raw_industry_slots and len(working_positions) > 0:
    slots_needed = raw_industry_slots - industry_slots
    # 当前防御持仓按评分从低到高排序，低分先卖
    current_defense.sort(key=lambda x: x[2])  # 按评分升序
    for t, shares, _ in current_defense:
        # ...卖出防御持仓...
        reason = '防御让路（腾槽位）'
```

**触发条件：**
- 有新的行业ETF BUY候选需要买入
- 但可用持仓槽位不足（已达 `max_total_holdings=5` 上限）
- 当前持仓中有防御资产（黄金/国债）

**执行逻辑：**
- 防御资产按评分从低到高排序，低分先卖
- 卖出防御资产以腾出槽位给行业ETF
- 卖出原因标注为：`"防御让路（腾槽位）"`

> 防御让路体现了策略的核心逻辑：优先配置股票敞口（行业ETF），当行业信号充足时，防御资产作为低优先级持仓被替换。

### 4.3 止损规则

| 参数 | 值 | 说明 |
|------|-----|------|
| `stop_loss_mode` | `fixed` | 固定止损模式 |
| `stop_loss` | -8% | 相对于成本价的亏损阈值 |
| `atr_stop_multiplier` | 2.0 | 仅当模式切换为ATR时生效 |
| `atr_period` | 14 | ATR计算周期 |

> 说明：ATR 动态止损在 Phase 6.5 中经诊断后**未采纳**，当前仍为固定止损 -8%。

### 4.4 防御填充规则

| 参数 | 值 |
|------|-----|
| `defense_enabled` | True |
| `defense_mode` | `mandatory`（强制配置） |
| `defense_fill_max_ratio_bull` | 30%（牛市时防御资产最多占总资产30%） |
| `defense_fill_max_ratio_bear` | 50%（熊市/弱市时防御资产最多占总资产50%） |

> 防御资产（黄金/国债）在股票 ETF 信号不足时作为低相关补仓资产填充仓位，不参与日常轮动排名。

### 4.5 宽基补仓规则

| 参数 | 值 |
|------|-----|
| `fallback_equity_enabled` | False（**已关闭**） |

> 回测显示当前参数下宽基补仓为负贡献，已关闭。如未来启用，需独立 A/B 验证。

### 4.6 持仓控制

| 参数 | 值 |
|------|-----|
| `stock_max_holdings` | 5（行业ETF最多持有5只） |
| `total_max_holdings` | 5（总持仓上限，向后兼容） |
| `max_position_per_etf` | 20%（单只上限20%，可用满） |
| `use_v2_rebalance` | True（v2.5 纯函数调仓规划逻辑） |

### 4.7 调仓频率

| 参数 | 值 |
|------|-----|
| `rebalance_weekday` | 3（周四） |
| `rebalance_freq` | `weekly`（每周） |

### 4.8 其他关闭的规则

| 规则 | 开关 | 状态 |
|------|------|------|
| 动态止盈 | `trailing_stop_mode = 'none'` | ❌ 关闭 |
| 冷静期 | `cooling_period = 0` | ❌ 关闭 |
| 大盘择时 | `market_timing = False` | ❌ 关闭 |
| 持仓稳定机制 | `STABILITY_CONFIG['enabled'] = False` | ❌ 关闭 |
| 市场状态自适应 | `MARKET_REGIME_CONFIG['mode'] = 'observer'` | ❌ 仅观察，不改参数 |
| 滑点 | `slippage_enabled = False` | ❌ 关闭 |

---

## 5. 交易执行口径

### 5.1 信号生成

- T 日收盘后，使用截至 T 日收盘时可获得的数据生成信号
- 所有指标使用 `shift(1)` 避免未来数据泄露
- 第 51 个交易日（索引 50）才是第一个完整指标集（MA50 经 shift(1) 后有效）

### 5.2 成交执行

- T+1 交易日开盘执行买卖
- 回测中使用当日 `open` 执行

> 注意：`src/config.py` 中 `EXECUTION_CONFIG['price_mode']='close'` 属于尚未接入当前回测执行路径的旧配置说明；实际基准成交口径以 `src/backtest.py` 使用当日 `open` 执行的实现为准。

### 5.3 佣金规则

| 参数 | 值 |
|------|-----|
| `commission_rate` | 0.03%（双向） |
| `min_commission` | 5.0 元 |

### 5.4 整手规则

- 100 股整手（实际由 `src/backtest.py` 中 `shares = int(target_value / price / 100) * 100` 实现）

### 5.5 滑点

- 当前**不计滑点**
- 原因：初始资金规模较小，对盘口冲击有限；已计入交易佣金，现阶段滑点不是最优先的模型误差来源
- 滑点测试属于未来稳健性验证，不能根据测试结果反向调优其他参数

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

## 7. 长期基准指标（已锁定）

以下指标来自**新鲜回测**（2026-06-21 17:34:40），与冻结报告 `reports/baseline_B0.3_20260620_180745.md` **完全一致**：

| 指标 | B0.3 值 | 来源 |
|------|---------|------|
| 最终 NAV | 2,809,091 | 新鲜回测 |
| 总收益 | 180.91% | 新鲜回测 |
| 年化收益 | 16.99% | 新鲜回测 |
| 夏普比率 | 0.8985 | 新鲜回测 |
| 最大回撤 | -17.75% | 新鲜回测 |
| 交易次数 | 801 | 新鲜回测 |
| 买入次数 | 398 | 新鲜回测 |
| 卖出次数 | 403 | 新鲜回测 |
| 调仓次数 | 337 | 新鲜回测 |
| 初始资金 | 1,000,000 | 配置 |
| 策略运行交易日 | ~1,500 | 估算 |
| 调仓截面数 | ~337 | 新鲜回测 |
| 平均持仓数 | ~3.5 | 估算 |

### 7.1 封存 OOS 指标（已冻结）

| 指标 | OOS 值 | 来源 |
|------|--------|------|
| 年化收益 | 42.16% | `reports/phase5_final_oos.md` |
| 夏普比率 | 1.9086 | `reports/phase5_final_oos.md` |
| 最大回撤 | -11.36% | `reports/phase5_final_oos.md` |
| 数据区间 | 2025-01-01 ~ 2026-06-18 | 已封存 |

> 该 OOS 区间已经封存，**不得继续用于参数选择或反向调优**。

---

## 8. 代码完整性校验

### 8.1 当前代码 Git SHA

```
884f529719f5a1e3bb4b9f043675c85ca3286f10
```

### 8.2 关键文件 SHA256

| 文件 | SHA256 | 大小 |
|------|--------|------|
| `src/config.py` | `0f66682261a632064d2d5836bc0a764274f735960ddd4be9ef9dad6967106afd` | 24,869 B |
| `src/strategy.py` | `7f5ba74385303037e5a61f225e9e582a277eabb41e46e4fd22ce1b29b6929a52` | 25,240 B |
| `src/backtest.py` | `8982ed4b81f5682cfd9053399814b57e41f2f43e280de93eb8825972d782edc0` | 106,251 B |
| `src/database.py` | `91f2cea50fd5864a63217b333fbb07db73c7d6f39a21bc95f05a32d2f84465c9` | 22,638 B |

### 8.3 配置哈希（可复现性）

以下关键参数的组合可唯一标识 B0.3 配置：

```python
# B0.3 配置指纹
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

## 9. 可复现的基准运行命令

### 9.1 生成 B0.3 基准报告（含 B0.2 对比）

```bash
# 在 D:\etf_rotation_model 目录下运行
py scripts/b0_3_baseline.py
```

预期输出：
- `reports/baseline_B0.3_时间戳.md` — B0.3 基准报告
- `reports/b0_2_vs_b0_3_时间戳.md` — B0.2 vs B0.3 精确对比报告
- 控制台应显示 `SUCCESS: B0.3 == B0.2 (exact match)`

### 9.2 单独运行回测（验证用）

```python
# 需要手动在 Python 中执行
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

### 9.3 关键指标验证 checklist

| 检查项 | 预期值 | 容差 |
|--------|--------|------|
| 最终 NAV | 2,809,091 | 精确匹配 |
| 总收益 | 180.91% | 精确匹配 |
| 年化收益 | 16.99% | ±0.01% |
| 夏普比率 | 0.8985 | ±0.0001 |
| 最大回撤 | -17.75% | ±0.01% |
| 交易次数 | 801 | 精确匹配 |
| 买入次数 | 398 | 精确匹配 |
| 卖出次数 | 403 | 精确匹配 |
| 调仓次数 | 337 | 精确匹配 |

如果任何指标超出容差，**停止并报告差异，不得直接更新本文件**。

---

## 10. 明确不属于 B0.3 的内容

以下内容存在于代码库中，但**不属于 B0.3 冻结基线**：

| 内容 | 状态 | 说明 |
|------|------|------|
| 32 只 ETF 池（概念 ETF） | ❌ 不属于 B0.3 | 16 只行业 + 16 只概念是研究框架，非生产基线 |
| `fixed_32` 回测结果 | ❌ 不属于 B0.3 | 不同标的池和信息集合，不能代表 B0.3 |
| 申万行业指数数据 (`SECTOR_INDEX_UNIVERSE`) | ❌ 不属于 B0.3 | v1.2/v1.3 研究用，未用于 B0.3 交易决策 |
| 市场状态 `active` 模式 | ❌ 不属于 B0.3 | `MARKET_REGIME_CONFIG['mode'] = 'observer'`，仅观察不改参数 |
| ATR 动态止损 | ❌ 不属于 B0.3 | Phase 6.5 诊断后未采纳，当前仍为固定止损 |
| 凯利仓位优化 | ❌ 不属于 B0.3 | Phase 8.1 诊断后未进入，当前仍为等权 |
| Phase 6 研究（市场结构、节假日、止损、持仓稳定） | ❌ 不属于 B0.3 | 诊断结果，未修改策略参数 |
| Phase 7 研究（幸存者偏差、ETF 池完整性） | ❌ 不属于 B0.3 | 诊断结果，未修改策略或 ETF 池 |
| Phase 8 研究（排名预测力、仓位实验） | ❌ 不属于 B0.3 | 诊断结果，未修改策略或仓位规则 |
| 滑点模拟 | ❌ 不属于 B0.3 | 未来稳健性验证，当前不计滑点 |
| 持仓稳定机制 | ❌ 不属于 B0.3 | `STABILITY_CONFIG['enabled'] = False` |
| 宽基补仓 (`FALLBACK_EQUITY_UNIVERSE`) | ❌ 不属于 B0.3 | `fallback_equity_enabled = False` |
| 动态止盈 | ❌ 不属于 B0.3 | `trailing_stop_mode = 'none'` |
| 冷静期 | ❌ 不属于 B0.3 | `cooling_period = 0` |

---

## 11. 基线更新流程

如需更新 B0.3 基线，必须：

1. **以 B0.3 为对照**开展独立 A/B 测试
2. **每次只修改一个变量**
3. **在训练期和验证期都优于 B0.3**，且风险未恶化
4. **新鲜回测验证**新配置与冻结报告的指标差异
5. **如果差异超出容差，停止并报告**，不得直接更新
6. **正式更新** `docs/B0_BASELINE_LOCK.md`、`docs/CURRENT_VERSION_NOTE.md`、`docs/CHANGES.md`
7. **提交 Git** 并记录变更历史

---

*本文档由 B0.3 正式基线锁定流程生成。变更历史参见 `docs/CHANGES.md`。*
