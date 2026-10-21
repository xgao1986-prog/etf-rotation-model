# 真实价格架构 v3 设计文档（Codex 复核两轮问题修复）

> 生成时间：2026-10-20
> 状态：实施中（先测试后实现；副本验证 → 正式库 → 交 Trae 定向复审；不提交不推送）
> 前置：DATA_UPDATE_FIX_v2_DESIGN.md（第二轮拼接修复，已被本文档修订）

---

## 1. 问题定性（Codex 两轮复核）

### 1.1 评分价与成交价混用（第一轮 BLOCKED 的架构级根源）

`_splice_ticker_with_fwd` 把**锚定换算后的等效价**（拆前口径）直接写入 `market_data.close`，
导致消费方无法区分：

| 消费方 | 需要的口径 | 修复前状态 |
|--------|-----------|-----------|
| 趋势评分（strategy.py：MA/动量/波动率） | 复权连续价（跨拆分无假跳变） | 用了等效价（碰巧连续但非标准复权） |
| 虚拟盘成交/估值/止损（paper_trading） | **真实市场价** | ❌ 用了等效价（股数、佣金、现金全部失真） |
| 实盘助手调仓脚本（live_generate_trade_plan.py） | **真实市场价** | ❌ 用了等效价 |
| 回测成交（backtest.py day_prices） | **真实市场价** | ❌ 用了等效价 |

### 1.2 成交量凭空放大（本轮新发现问题）

拼接时 `volume = iFinD volume × 锚定因子`。锚定因子 512480 = 2.0、515880 ≈ 2.0，
**全部拼接行成交量被放大一倍**（512480 常态 8 亿份 → 存 16~50 亿份）。
`volume_ratio = volume / MA20(volume)` 被人为抬升 → `volume_score` 虚高 → `total_score` 失真。
实测证据：512480 修复前 6/30~7/10 存量 16.5~50.4 亿，6/22~6/29 真实量 7.5~9.7 亿。

## 2. 架构方案：价格双轨制

### 2.1 market_data 列语义（新标准）

| 列 | 语义 | 用途 |
|----|------|------|
| open/high/low/close | **真实市场价**（当日实际成交价，单位元） | 交易、估值、止损、成交模拟 |
| volume | **真实成交量**（统一单位：份，股） | 展示；评分成倍调整的基础 |
| adj_close | **前复权连续收盘价**（拆分+分红调整，iFinD forward 口径锚定到库内尺度） | 趋势评分唯一价格来源 |
| adjust_type | 'real' / 'real_spliced'（真实价）；历史遗留行保持原标记不动 | 审计 |

### 2.2 评分成倍规则（strategy 输入准备层做，strategy.py 不改）

对输入 df 计算 `adj_factor = adj_close / close`（无 adj_close 或全等 1 → 原样返回，**冻结路径行为零变化**）：

- 评分用价格：`open/high/low/close ×= adj_factor`（真实→连续）
- 评分用量：**`volume ÷= adj_factor`**（= volume × close/adj_close）

量级论证（512480，1:2 拆分 7/3 生效）：
- 价格轨：拆分前 adj_factor=0.5，价格折半 → 7/2→7/3 收益率连续（-50% 假跳变消失）
- 成交量轨：拆分前 volume ×2（折股后等效份数）→ 7/3 真实放量 2 倍 ÷ MA20(等效基准 2 倍) ≈ 1
  → **volume_ratio 连续，不制造虚假放量评分**
- 非拆分日期 adj_factor=1 → volume 原样，未套用任何价格换算倍数

### 2.3 拆分事件表 corporate_actions

```sql
CREATE TABLE IF NOT EXISTS corporate_actions (
    ticker TEXT NOT NULL,
    effective_date TEXT NOT NULL,   -- 拆分后首个交易日
    action_type TEXT NOT NULL,      -- 'split' / 'dividend'
    ratio REAL NOT NULL,            -- 拆分比：1 旧股 → ratio 新股
    source TEXT, note TEXT,
    PRIMARY KEY (ticker, effective_date)
)
```

检测：iFinD none vs fwd 同日比率 `none/fwd` 的阶跃（|阶跃-1|>20% 判拆分；0.3%~20% 判分红，持久 ≥5 日确认）。
用途三路：(a) 全历史 adj_close 重算；(b) 虚拟盘持仓跨拆分调整；(c) detect_splice_breaks 排除真实拆分日。

### 2.4 各消费方改造

| 文件 | 改动 |
|------|------|
| src/data_fetcher.py | `_splice_ticker_with_fwd` 重写：拉 none（真实）+ fwd（连续）双序列；存真实 OHLCV + adj_close=fwd×adj校准因子（锚=库内 adj_close(锚日)，保持尺度衔接）；拆分检测写 corporate_actions；**volume 存真实值，不再乘任何因子**；`detect_splice_breaks` 排除已知拆分日 |
| src/database.py | 新增 corporate_actions 表 + `save_corporate_actions` / `get_corporate_actions`；新增 `apply_adjustment_for_scoring(df)` 静态工具（2.2 规则，无 adj_close 时恒等返回） |
| app.py `_recalculate_scores` | 输入 df 先过 `apply_adjustment_for_scoring` 再进 StrategyEngine |
| src/backtest.py `run()` | 评分输入用调整后副本；`day_prices`/成交/NAV 用真实 close。**冻结路径（无 adj_close 列）恒等不变**——B0.4 测试只喂 7 列 CSV，已核实 |
| src/paper_trading/runner.py + store.py | run_daily 增加第 0 步 `apply_corporate_actions`：拆分生效日盘前，持仓 shares×=ratio、cost_price/=ratio、last_price/=ratio，NAV 不变；存调整记录 |
| scripts/live_generate_trade_plan.py | 无需改：读 close=真实价后自动正确（核查确认） |
| 虚拟盘 UI data_provider | 无需改：读 open/close=真实价后自动正确 |

## 3. 数据迁移（先副本后正式库）

`scripts/migrate_real_prices_v3.py`（幂等）：

1. 备份正式库
2. 19 只全历史分段拉 iFinD none+fwd（≤3 年/段）→ 检测拆分/分红 → 写 corporate_actions
3. 删除 2026-06-30 起 `source='iFinD-KimiGateway'` 的全部拼接行（1389 行），
   用 none 真实 OHLCV + fwd 校准 adj_close 重生成；volume 用 iFinD 真实份数
4. 全历史 adj_close 重算：`adj_close(t) = close(t) × fwd(t)/none(t)`（同日比率，历史 close 为真实价已验证）
5. 515880 的 7/6~7/10 correction 行：close/volume ÷2 转真实价（这些行此前被 ×2 成拆前口径）
6. 重算 daily_scores（B0.4 标准配置）
7. 验证包：B0.4 快照 SHA-256 不变；512480 6/29~7/3 价格/成交量/volume_ratio/volume_score 对照表；
   拆分边界收益率连续；虚拟盘三类场景测试；幂等重跑 saved=0

## 4. 代表性验证（TDD，先写测试）

1. **拆分后新建仓**：虚拟盘在 7/3（拆分后）按真实开盘价买入 → 成交价=市场价、
   股数按真实价取整手、佣金=max(股数×价×费率, 5)、现金扣减精确
2. **持仓跨拆分**：7/2 持仓 1000 股 @成本 2.70 → 7/3 盘前调整为 2000 股 @成本 1.35，
   市值与总资产不变（不腰斩不虚增）
3. **成交量不造假**：512480 6/29~7/3 的 volume_ratio 序列在拆分边界无 ≥1.5 倍跳变；
   存储 volume 与 iFinD none 真实值逐日相等

## 5. 表述修正

"盘中快照冒充收盘"证据不足以定性 → 全部文档与代码注释改为
**"旧数据与核验来源不一致，原因尚待确认"**。

## 6. 明确不做

- 不动 B0.4 冻结快照（SHA-256 校验）、冻结指标、交易规则、策略参数
- 不改 strategy.py 评分逻辑本身（只改输入准备）
- 不提交不推送；AKShare fallback 路径保持"未独立验证"披露
- 历史遗留 adjust_type 标记行（AKShare 时代）不改动 OHLCV（ adj_close 重算除外，属新增列语义修正）
