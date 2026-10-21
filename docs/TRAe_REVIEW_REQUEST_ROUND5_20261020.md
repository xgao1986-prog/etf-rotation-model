# Trae ROUND4 验收问题修复：BacktestEngine 公司行动同步 + 页面基准口径 + 8月亏损分析

> **ROUND5 补充（2026-10-21）**：按用户三项意见完成第二轮收口，本文第五节
> "页面区间大盘收益比较"的**旧结论作废**——年度表原算法（全期累计 bench_return
> 列差分）**确实有错**，已修复，见文末"ROUND5 补充"章节。

## 一、问题复现（先证明错误存在）

**复现脚本**：`scripts/reproduce_bt_split_bug.py`（窗口 2025-11-01 ~ 2026-07-20，正式库真实价）

复现结果（修复前）：

```
2026-07-02  512480.SH  STOP_LOSS  1.650  固定止损: 成本盈亏-10.52%  ← 515880 真亏损，正常
2026-07-03  512480.SH  STOP_LOSS  1.322  固定止损: 成本盈亏-51.04%  ← 拆分价差误读为亏损！
2026-07-03  NAV 日收益 -9.90%（虚假下跌）
```

根因：真实价格架构 v3 后 `market_data` 存真实价，512480 于 2026-07-03 进行 1:2 拆分（上交所公告：7-02 登记 / 7-03 除权），真实价从 2.700 跳至 1.331。**虚拟盘 runner 有 `_apply_corporate_actions` 同步持仓（shares×2、cost÷2），但 BacktestEngine 没有对应逻辑**——持仓成本仍是拆前口径 2.7+，拆分日开盘价 1.322 被算成 -51% 亏损，触发 -8% 固定止损，组合当日虚假下跌 9.9%。

## 二、修复内容

### 2.1 BacktestEngine 公司行动同步（核心修复）

`src/backtest.py`：

- `run()` / `_execute_backtest()` 新增 `corporate_actions` 参数（缺省 None，行为与 v6.2 完全一致——**冻结基线路径零变化**）
- 每日盘前（止损检查之前）应用当日生效的 confirmed 记录：
  - `shares ×= ratio`、`cost ÷= ratio`、`high_water ÷= ratio`、`atr_at_entry ÷= ratio`
  - 市值与总资产不变（价格已是拆后真实口径）；成本同比例调整使止损判断不受拆分影响
- **确认闸门与虚拟盘 runner 同一语义**：仅 `status=='confirmed'` 可调整持仓；pending 记录当日对该标的跳过止损（防未确认拆分的价差误触发）；ratio 非法不调整也不止损
- `run_in_sample()` / `run_out_sample()` 同步透传

调用方接入：

- `main.py cmd_backtest`：从 `db.get_corporate_actions(status='confirmed')` 取记录传入三个回测分支
- `app.py run_weighted_backtest`：同上（页面回测路径）

### 2.2 回归测试

新增 `tests/test_backtest_corporate_actions.py`（先红后绿）：

- `test_no_stop_loss_on_split_day`：拆分日 512480 不得出现止损交易
- `test_no_fake_nav_drop_on_split_day`：拆分日 NAV 日收益不得 <-5%

修复前：2 errors（`run()` 无 `corporate_actions` 参数，复现脚本已实证 -51% 误止损）；修复后：**2 passed**。

### 2.3 修复效果

| 日期 | 修复前 NAV 日收益 | 修复后 NAV 日收益 |
|---|---|---|
| 2026-07-03（512480 拆分日） | **-9.90%**（虚假） | **+0.17%**（真实） |

## 三、B0.4 冻结基线验证（保留）

`tests/test_b0_4_slippage.py`：**9 passed**（未动冻结快照，SHA-256 `dbdb041d…` 校验通过；0bp 仍精确复现 NAV 2,761,288.07 ±1.0 + 804 笔交易）。冻结路径不传 `corporate_actions` → 引擎行为与冻结时逐字节一致。

## 四、近期区间重跑（修复后，2026-01-01 ~ 2026-10-20）

- 期末 NAV 1,001,350，总收益 +0.14%，66 笔交易
- 止损 3 次，全部为真实亏损（非拆分误触发）：
  - 2026-07-02 515880 成本盈亏 -10.52%（真跌）
  - 2026-07-16 512480 成本盈亏 -11.78%（拆分后真实下跌，拆分日本身已正确同步）
  - 2026-09-29 515880 成本盈亏 -10.92%（真跌）
- **全区间无单日跌幅 >5%**（修复前 7/3 单日 -9.9%）
- 数据存 `reports/recent_2026_nav_fixed.csv` / `recent_2026_trades_fixed.csv`

## 五、页面区间大盘收益比较（统一核对）

发现问题：页面选定区间后，指标卡只显示策略数据，大盘收益无同时段对比；年度表用 `bench_return` 列（全期累计口径）差分，与选定区间卡片的重新归一口径不一致。

修复（`app.py`）：

- 选定区间指标卡新增两行：**"沪深300收益（同时段）"** 与 **"超额收益（策略-大盘）"**
- 用 `filtered_nav["bench_price"]` 直接计算（首末日价格比），与净值图 `make_combined_figure` 的区间归一化**同基期同口径**
- 年度表的 bench 差分逻辑保持不变（年度窗口内自洽）

## 六、8 月持仓亏损分析（只分析，未调参数）

**区间表现**：8 月策略 **-4.84%**（1,105,821 → 1,052,261），同期沪深300 **+1.80%**，跑输 6.6 个百分点；月内最大回撤 -5.33%。

**亏损结构**（8 月 8 笔卖出，FIFO 粗配对）：

| 卖出日 | 标的 | 实现盈亏 | 幅度 |
|---|---|---|---|
| 08-20 | 515230 软件ETF | **-15,513** | **-7.58%** |
| 08-20 | 512980 传媒ETF | **-14,512** | **-7.09%** |
| 08-27 | 512010 医药ETF | -9,313 | -4.55% |
| 08-20 | 159928 消费ETF | -1,955 | -0.96% |
| 08-27 | 516160 新能源ETF | -1,399 | -0.68% |
| 其余 3 笔 | — | 小幅盈利 | — |

三笔大亏损合计 -39,338，占 8 月亏损的绝对主力。

**共性模式（追高买入 → 动量熄火 → 一周调出）**：

1. **软件ETF（515230）**：8/6 买入 @0.744，此前 5 个交易日从 0.677 连涨至 0.754（**+11% 急涨**），买入即短期高点；8/20 调出 @0.691。全月标的本身仅 -2.07%，**-7.58% 的亏损几乎全部由追高高买造成**（买入价比月内大部分时间都高）。
2. **传媒ETF（512980）**：8/6 买入 @0.870，此前 7/28→8/5 从 0.771 涨至 0.881（**+14% 急涨**），同样买在动量顶点；8/20 调出 @0.812。标的全月仅 -1.41%。
3. **医药ETF（512010）**：8/20 买入 @0.401（此前 8/4~8/7 从 0.383 跳涨 +5%），8/27 调出 @0.383。

**机制归因**（与 v1.2.2 市场质量门同一类问题）：8 月初软件/传媒的急涨使动量评分冲顶入选，但急涨后的次日动能未能延续（8/6 后两标的横盘回落），评分跌出候选列表即被"调出候选列表"规则卖出——**策略为一周内的动量反转支付了高买低卖的完整价差**。值得注意的是，8 月也有 512400 有色（+9.84%）、515880 通信（+15.46%）等标的期间大涨，但策略持仓中获利兑现的路径（持有至调出）使这部分收益未能覆盖软件/传媒的追高亏损。

**观察（仅记录，不改参数）**：
- 三笔亏损买入的共同点：买入前 5 日涨幅 >5%，呈"急涨追入"形态；可考虑的研究方向（ backlog）：入场前 N 日涨幅过大的降权/冷却规则
- 8/6 三笔同时新买入（515230/512400/512980）后 2 周内 2 笔亏损调出，同期组合集中度风险值得留意

## 七、全量回归

`pytest tests/ -q`：383 passed / 2 failed——两个失败均为基线既有问题（`test_missing_data_antipattern`、`test_v1_3_step6 regime 分布`），与本轮改动无关。加上单独跑过的 B0.4 滑点（9 passed）和本修复（2 passed），共 394 passed。

## 八、改动文件清单（未提交未推送）

| 文件 | 改动 |
|---|---|
| `src/backtest.py` | `run()`/`_execute_backtest()` 加 `corporate_actions` 参数；盘前 confirmed 拆分/合并同步（shares/cost/high_water/atr）；pending 当日跳过止损；`run_in_sample`/`run_out_sample` 透传 |
| `main.py` | cmd_backtest 三个分支传入 confirmed 公司行动 |
| `app.py` | `run_weighted_backtest` 传入 confirmed 公司行动；选定区间新增同时段沪深300收益与超额指标卡 |
| `tests/test_backtest_corporate_actions.py` | 新增：拆分日无误止损 + 无虚假跳跌（2 项） |
| `scripts/reproduce_bt_split_bug.py` | 新增：问题复现脚本 |
| `reports/recent_2026_nav_fixed.csv` / `recent_2026_trades_fixed.csv` | 近期区间修复后回测输出 |

## 九、请 Trae 复审的问题

1. **回测与虚拟盘的公司行动语义是否完全对齐**（confirmed 调整、pending 跳止损、ratio 校验）？
2. **冻结基线路径**（不传 corporate_actions）确认零行为变化是否正确——即冻结快照数据本身不需要拆分同步？
3. **atr_at_entry ÷= ratio** 的处理：ATR 止损价随成本同比例缩放，是否与你对 ATR 止损跨拆分的预期一致？
4. **页面基准口径**：选定区间卡片用 bench_price 首末比，年度表用 bench_return 差分——两个口径各自自洽，是否需要统一为同一个？

---

# ROUND5 补充（2026-10-21）：基准口径统一 + pending 保护接通 + 拆分测试加强

## 十、修正声明：不再沿用"年度表原算法正确"的结论

第五节曾写"年度表的 bench 差分逻辑保持不变（年度窗口内自洽）"——**该结论错误，作废**。

年度表原算法 `bench_end - bench_start`（全期累计 bench_return 列差分）的数学本质是
`(年末-年初)/全期首日`，**不是**该年度的 `年末/年初 - 1`。全期首日与年度首日
每差 1% 市场涨幅，年度基准收益就被系统性扭曲约 1 个百分点（上涨年虚高、下跌年虚低）。
选定区间图的 `1 + (bench_return - bench_return_start)` 归一同理有误。

## 十一、意见一：图表与年度表统一按各自区间计算基准收益

**统一口径**：所有比较区间（选定区间 / 单一年度 / 全期）一律按**区间内
bench_price 首末比 - 1** 计算，各自独立基期，互不借用。

- 新增公共函数 `app.bench_return_in_range(nav_df)`，四处共用：
  选定区间指标卡、合并图基准归一、净值图基准线、年度表
- 年度表由全期累计列差分改为按年份分组调用上述函数
- 合并图/净值图由 `1 + bench_return`（全期基期）改为 `bench_price / 区间首日价`

**非全期区间测试** `tests/test_bench_return_unified.py`（5 项全过）：

1. 全期区间：与 bench_price 首末比一致
2. **非全期选定区间**：切片内首末比，且回归锁定——旧差分口径在子区间上与本口径不等价（防回退）
3. 非完整年度（跨年头尾各缺一段）：年度分组各自独立
4. 年度组与同年切片的一致性
5. NaN/缺列/空表/None 边界

## 十二、意见二：接通待确认公司行动的保护（页面 + CLI 均有效）

**发现的问题**：回测引擎虽有 pending → 当日跳过止损的保护逻辑，但 `main.py`
和 `app.py` 都只查 `status='confirmed'` 传参——pending 记录永远到不了引擎，
保护是**死代码**。

**修复**：两个入口改为 `db.get_corporate_actions()` 传**全量记录**（confirmed
+ pending），引擎内部分流：confirmed 调整持仓；pending 由 ROUND6 安全处理
统一裁决（见第十六节，已取代旧语义的"当日跳过止损继续跑"）。

**入口有效性验证**（拦截测试，非源码 grep）：
`TestEntryPointsPassAllActions` 用 monkeypatch 拦 `cmd_backtest` 与
`run_weighted_backtest` 的数据加载和引擎调用，断言传入的 `corporate_actions`
与 `db.get_corporate_actions()` 全量结果**逐条一致**（CLI、页面各 1 项）。

**pending 行为测试** `TestPendingProtectionLive`（真实窗口重跑，ROUND6 新语义）：
1. pending 拆分涉持仓 → 回测抛 `PendingCorporateActionError` 整体暂停，不输出任何结果
2. pending 拆分未持仓 → 该标的新买入资格暂停直到确认（真实窗口中验证无后续 BUY）

## 十三、意见三：拆分测试加强（全链条数字验证）

`TestBacktestSplitPositionChain` + 既有用例，共 14 项全过，覆盖：

| 验证点 | 断言方式 | 结果 |
|---|---|---|
| 拆分前持仓 | 06-25 最后一次买入存在、价格=2.700（真实价） | ✅ |
| 拆分后股数 | 拆分后止损股数 = 拆前股数 **×2**（夹具内实际值，非硬编码） | ✅ |
| 拆分后成本 | 止损价 1.191 / 拆后成本 1.350 - 1 ≈ **-11.8%**，与止损理由记录值一致（拆前口径则 ≈ -56%） | ✅ |
| 资产金额 | 拆分日 NAV 变化在 **±1%** 内（无跳变） | ✅ |
| 拆分日无误止损 | 7/3 无 512480 STOP_LOSS | ✅ |
| 正常涨跌仍止损 | 7/16 真实下跌触发止损（股数=2N，盈亏按拆后成本） | ✅ |
| pending 不误调整/不误止损 | 见第十二节 | ✅ |

## 十四、重跑结果

| 测试 | 结果 |
|---|---|
| `test_backtest_corporate_actions.py`（拆分全链条+pending+入口） | **14 passed** |
| `test_bench_return_unified.py`（非全期区间口径） | **5 passed** |
| `test_b0_4_slippage.py`（B0.4 冻结基线） | **9 passed**（NAV 2,761,288.07 + 804 笔，快照未动） |
| 近期回测（2026-01-01~10-20，全量公司行动路径） | 与修复后一致：NAV 1,001,350 / 66 笔 / **拆分日 0 止损** / 无 >5% 单日下跌 |
| 其余全量回归 | 383 passed / 2 failed（均为基线既有问题） |

## 十五、请 Trae 复审的问题（ROUND5 更新）

1. **统一口径的正确性**：`bench_return_in_range` 按各自区间首末比——年度表与选定区间卡现在可能在同一区间上给出相同数字（此前不一致），请确认这就是你要求的"统一核对"
2. **pending 跳止损的一天窗口**：引擎只保护行动当日，次日价格仍是拆前成本口径下的 -50%，会立即触发止损（测试已确认）——这个"只保一天"的语义是否符合预期？
3. **入口传全量记录**：CLI 与页面都传 confirmed + pending，引擎内分流。是否同意"入口不做过滤、引擎统一裁决"的分层？

# ROUND6 补充（2026-10-21）：待确认公司行动安全处理升级

## 十六、修复内容（冻结快照与策略参数不变）

**旧语义的问题**：ROUND5 时 pending 的待遇是"当日跳过止损、继续输出收益"
——若拆分真实发生而股数未确认，继续运行会基于错误的持仓口径产生虚假
盈亏、错误止损和错误资金分配，且把错误状态存进账户。

**新语义（按你的要求）**：

| 场景 | 裁决 | 行为 |
|---|---|---|
| pending 涉持仓（含 confirmed 但 ratio 非法） | **暂停** | 回测抛 `PendingCorporateActionError`，不输出收益/交易；虚拟盘返回 paused 结果，**不保存当日状态**（当日保持未处理，确认后重跑恢复） |
| pending 未持仓 | **禁买** | 该标的新买入资格暂停，跨交易日持续直到确认为止 |
| confirmed + 合法 ratio | 正常 | 生效日调整 shares/cost/high_water/atr，冻结路径缺省 None 零变化 |

**实现位置**：
- `src/backtest.py`：`PendingCorporateActionError` 异常类；pending 涉持仓在
  每日循环止损检查之前 raise；未持仓加入 `buy_suspended` 集合，当日
  `day_signals` 中剔除（覆盖全部买入路径）；`blocked_by_date` 旧逻辑删除
- `main.py` / `app.py`：入口 try/except 捕获，CLI 打印暂停横幅后 return，
  页面返回 `{"error": "回测已暂停（待确认公司行动）：..."}`
- `src/paper_trading/runner.py`：`_apply_corporate_actions` 返回
  `(state, pause_info, suspended_buy)`；`run_daily` 收到 pause_info 直接返回
  `_build_paused_result`（无交易/无估值/无 save_daily_state）；suspended_buy
  传入 `_build_signal_orders`，BUY 订单剔除并留痕
- `app.py _load_corporate_actions`：返回当日生效记录 + **所有 pending 记录**
  （跨交易日持续禁买的前提）
- `src/paper_trading/ui_service.py`：paused 结果归入 `skipped` 并显示原因

## 十七、跨两个交易日测试（无虚假亏损/错误止损/错误资金分配）

`tests/test_real_price_adjustment.py` 新增：

1. **`test_pending_action_pauses_account_run`**：7/3 pending 涉持仓 →
   paused、无交易无订单无信号、持仓保持拆前口径、**当日未标记已处理**、
   DB 无 7/3 记录；置 confirmed 后重跑同一日 → 正常调整（2000 股 @ 2.70
   成本）、NAV 无亏损（1,000,000 = 1000×5.40 = 2000×2.70）
2. **`test_pending_cross_two_days_no_false_state`**：7/3 与 7/6 两天均未
   确认 → 两天都 paused、无 STOP_LOSS 落库、现金保持 994,600、无 NAV 记录、
   无资金分配；确认后重跑 7/3 NAV 连续
3. **`test_pending_not_held_suspends_buy_until_confirmed`**：未持仓 pending
   第 1 天（生效日）与第 2 天（已过生效日）都在 suspended_buy 中，确认后
   解除；涉持仓 pending 触发 pause；未来生效日暂不处理

`tests/test_backtest_corporate_actions.py` 改造：

4. **`TestPendingProtectionLive`** 三项：pending 涉持仓 →
   `pytest.raises(PendingCorporateActionError)`；pending 未持仓 → 真实窗口中
   该标的之后无任何 BUY；暂停的运行不产生任何输出

## 十八、重跑结果（ROUND6）

| 测试 | 结果 |
|---|---|
| `test_real_price_adjustment.py`（含 3 项新跨日用例） | 17 passed |
| `test_backtest_corporate_actions.py`（新语义） | 10 passed |
| `test_bench_return_unified.py` | 5 passed |
| `test_b0_4_slippage.py`（B0.4 冻结基线） | 9 passed（NAV 2,761,288.07 + 804 笔，快照未动） |
| 全量回归 | 409 passed / 2 failed（均为基线既有问题，与改动无关） |
| CLI 全样本回测（9 条 confirmed 全量路径） | 正常完成：总收益 140.99%、927 笔、无暂停 |

## 十九、请 Trae 复审的问题（ROUND6 更新）

1. **暂停 vs 跳止损的取舍**：新语义下 pending 涉持仓直接暂停整个回测/
   账户（旧"当日跳过止损继续跑"已废弃）——确认这是你要的安全等级？
2. **跨日禁买的持久化**：页面 provider 每次返回当日记录 + 所有 pending
   记录，pending 未确认期间该标的一直禁买；确认后自动恢复。这个持久化
   方式是否可接受？
3. **确认后恢复路径**：暂停的当日不标记已处理，确认后重跑同一日即可
   从断点恢复（虚拟盘）；回测则整段重跑。是否同意这两种恢复方式？

## 二十、ROUND6 补漏：恢复与异常场景（两个，不扩大范围）

用户认可暂停方向后，仅补齐以下两个场景，冻结快照与策略参数不变。

### 补漏①：生效日暂停 → 隔日确认 → 直接运行隔日 → 必须阻止

**风险**：7/3 因 pending 暂停（未保存）；7/6 记录已确认，若直接续跑，
基线仍是生效前旧股数（1000 @ 5.40），面对 7/6 真实价 2.72 即 -50%
虚假亏损，并立刻触发 -8% 错误止损，还会把错误状态保存进账户。

**修复**（`src/paper_trading/runner.py`）：
- 新增 `_find_missed_effective_date`（5.4 步，基线加载后、公司行动处理前）：
  confirmed 记录且生效日 < 当日、账户持仓该标的、生效日 ≥ 账户起点、
  且生效日 `is_day_processed` 为 False → 返回 missed 信息
- `_build_paused_result` 新增 missed 分支文案：明确提示"生效日 X 尚未处理，
  请先补跑 X（确认后重跑该日会正常调整股数），再运行当日"
- 与 pending 暂停同一安全等级：无交易、无估值、无保存、当日不标记已处理

**测试** `test_confirmed_missed_effective_date_blocks_jump`：
7/3 pending 暂停 → 7/6 带 confirmed 直接运行 → 暂停且文案含"补跑"、
无交易/无 NAV/未标记已处理、持仓仍旧口径；补跑 7/3（2000 股 @ 2.70 成本）
→ 7/6 正常恢复，2.72 vs 2.70 = +0.7% 不触发止损，NAV 连续。

### 补漏②：公司行动记录查询失败 → 停止本次运行并明确报错

**风险**：`app.py _load_corporate_actions` 原来查询失败时静默返回 None，
ui_service 把异常也吞成 None——查询失败被当作"没有记录"继续运行，
未确认拆分会静默漏掉盘前调整。

**修复**：
- `app.py _load_corporate_actions`：删除 try/except 静默，异常直接上抛
- `src/paper_trading/ui_service.py run_accounts`：provider 抛异常时返回
  `failure`（"公司行动记录查询失败，本次运行已停止：…"），不运行任何账户

**测试** `test_corporate_actions_query_failure_stops_run`：provider 抛
RuntimeError → `results["failure"]` 含明确报错、`success` 为空、
无交易、无 NAV、未标记已处理。

### 重跑结果（ROUND6 补漏）

| 测试 | 结果 |
|---|---|
| `test_real_price_adjustment.py`（19 项，含 2 项新场景） | **19 passed** |
| `test_backtest_corporate_actions.py` + `test_bench_return_unified.py` | **15 passed** |
| 全量回归（含 B0.4 冻结基线 9 项） | **411 passed / 2 failed**（均为基线既有问题） |

### 请 Trae 复审的问题（ROUND6 补漏）

1. **跳期防护的边界**：只拦"confirmed + 持仓该标的 + 生效日 ≥ 账户起点 +
   生效日未处理"四种条件同时成立的场景；未持仓标的不拦（无旧股数风险）、
   账户起点前的生效日不拦（起点为人工导入口径）。边界是否完整？
2. **查询失败的呈现**：provider 异常现在会让当日所有选中账户都进
   `failure`（同一报错文案）。是否同意"一次查询失败、全部停止"，
   而不是逐账户重试？

### 补漏③：页面查询必须包含历史 confirmed 记录（恢复路径闭环）

**发现的问题**：`app.py _load_corporate_actions` 原来只返回"当日生效记录 +
全部 pending"。跳期防护（5.4 步）需要**已确认的历史记录**才能判断"生效日
是否已处理"——隔日确认后直接运行隔日时，页面查询返回空，防护拿不到那条
confirmed 记录，账户带着旧股数恢复运行（2.72 vs 成本 5.40 = -49.6%，
立刻错误止损并保存错误状态）。手工传完整记录的单元测试无法发现这个
入口层面的丢失。

**修复**（`app.py _load_corporate_actions`）：改为返回
- 生效日 ≤ 当日的**全部**记录（含历史 confirmed——跳期防护的必需输入）
- 所有 pending 记录（跨日禁买，不变）

查询失败仍直接抛异常（补漏②语义不变）。

**完整流程测试** `test_page_query_entry_full_flow_backfill_required`：
不绕过页面查询——用 `app._load_corporate_actions` 作为 provider 注入
PaperTradingUIService，仅 monkeypatch `app.ETFDatabase` 指向临时库，
公司行动记录全部通过 `save_corporate_actions` / `confirm_corporate_actions`
（生产同款入口）写入。流程：

1. 7/2 正常建立状态（pending 未来生效日记录也返回，但不触发）
2. 7/3 pending 涉持仓 → 页面路径暂停（skipped，无状态保存）
3. 库中 confirm 后，**断言 provider("2026-07-06") 仍返回该 confirmed 记录**
   （历史 confirmed 不可丢——本补漏的核心断言）
4. 7/6 直接运行 → 必须提示"先补跑 2026-07-03"，无交易/无 NAV/未标记
5. 补跑 7/3（同样走页面查询）→ 2000 股 @ 2.70 成本
6. 再跑 7/6 → 正常继续：无暂停、无错误止损、NAV = 994,600 + 2000×2.72

### 重跑结果（补漏③）

| 测试 | 结果 |
|---|---|
| `test_real_price_adjustment.py`（20 项，含页面入口完整流程） | **20 passed** |
| `test_backtest_corporate_actions.py` + `test_bench_return_unified.py` | **15 passed** |
| 全量回归（含 B0.4 冻结基线 9 项） | **412 passed / 2 failed**（均为基线既有问题） |

### 请 Trae 复审的问题（补漏③追加）

1. **页面查询返回集的口径**：现在生效日 ≤ 当日的记录全量返回（9 条规模，
   无性能问题）。是否同意"按日期上限过滤"而不是"只返回当日 + pending"？
2. **测试注入方式**：流程测试 monkeypatch 的是 `app.ETFDatabase`（数据
   库连接指向临时库），页面查询函数本身原样执行。是否认可这算"通过页面
   实际查询入口"，而非绕过？
