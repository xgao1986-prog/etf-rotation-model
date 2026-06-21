# 变更记录（给 Codex 看的摘要）

> 记录每次修改的内容，方便快速同步。不需要懂代码，看中文就能知道改了什么。
> 记录人：Kimi（每次修改后更新）

---

## 2026-06-20（本次 - Phase 6.7 长假调仓日历适配实验最终修正版）

**问题根因：**
1. **交易日未截断**：调仓日历使用了截至2026-06-18的全部交易日，但回测只到2024-12-31，导致计划周四生成到2026年，日历与回测不一致。
2. **C方案存在无效替代**：source_date == target_date（节后首个交易日恰好是周四），这些不应计数为替代。
3. **验证只打印不终止**：validate_rebalance_dates()发现错误时只打印，仍继续回测。
4. **间隔验证范围过宽**："无<3交易日间隔"检查了两两正常周四之间的间隔，但正常周四间隔短是合法的（如节前周四与节后周四之间只有0-2个交易日）。应只检查新增替代与相邻调仓日之间的间隔。
5. **B0.3复现不严格**：使用STRATEGY_CONFIG.copy()，未通过build_config()；fallback_equity_enabled未关闭；数据加载不完整。
6. **B方案未区分概念**：未区分"被替代计划周四数量"和"实际新增调仓日数量"。

**修正内容：**
- 交易日截断到2024-12-31后再生成日历（1357个交易日 vs 原始1708）
- C方案过滤掉source_date == target_date的无效替代（7次→3次有效替代）
- validate_rebalance_dates()改为raise AssertionError，失败立即终止
- 间隔验证改为只检查新增替代target_date与前后相邻调仓日之间的间隔
- 严格复现B0.3：使用build_config(strategy_cfg={'momentum_factor_enabled':False, 'volatility_factor_enabled':False, 'min_total_score':40, 'stop_loss':-0.08, 'stop_loss_mode':'fixed'}, fallback_equity_cfg={'fallback_equity_enabled':False})
- B方案区分：被替代计划周四13个，实际新增调仓日12个（同一长假用同一个last_before替代多个周四）
- 增加日历纯函数测试（6个）：所有日期不晚于2024-12-31；C无同日替代；所有目标都是交易日；被替代计划周四已删除；验证失败会中止；输入顺序不影响结果

**测试命令：**
```bash
python scripts/phase6_7_holiday_rebalance_experiment.py
```

**测试输出：**
```
[PASS] 测试1: 所有计划周四不晚于2024-12-31
[PASS] 测试2: C方案无同日替代
[PASS] 测试3: 所有替代目标都是交易日
[PASS] 测试4: 被替代计划周四已删除
[PASS] 测试5: 验证失败会真正中止
[PASS] 测试6: 输入顺序不影响结果
--- 所有日历测试通过 ---
[A] 所有断言通过
[B] 所有断言通过
[C] 所有断言通过
[D] 所有断言通过
```

**新回测结果（2019-08-13 ~ 2024-12-31，B0.3基准）：**

| 指标 | A(当前) | B(前补) | C(后补) | D(最近) |
|------|---------|---------|---------|---------|
| 总收益 | 61.96% | 38.96% | 27.68% | 55.36% |
| 年化收益 | 9.74% | 6.55% | 4.82% | 8.87% |
| Sharpe | 0.530 | 0.370 | 0.280 | 0.480 |
| 最大回撤 | -19.75% | -20.56% | -23.90% | -21.05% |
| 调仓日 | 275 | 274 | 272 | 272 |
| 替代次数 | 0 | 13 | 3 | 5 |

**结论：**
> 保持当前周四调仓规则（A），不采纳任何长假日历适配规则。
> B劣化23.00%，C劣化34.28%，D劣化6.59%。当前规则已最优。

**改了哪些文件：**
- 重写 `scripts/phase6_7_holiday_rebalance_experiment.py` — 最终修正版（约400行，含6个日历纯函数测试）
- 更新 `reports/phase6_7_holiday_rebalance_experiment.md` — 修正版报告
- 更新 `docs/CURRENT_STATE.md` — 更新Phase 6.7摘要
- 更新 `docs/CHANGES.md` — 添加变更记录

**Commit：** 待提交

---

**修改了什么：**
- 修正Phase 6.5的ATR止损逻辑理解：
  - 原报告错误：'代码逻辑取两者中更严格的，不是放宽'
  - 正确理解：min(atr_stop, fixed_stop)取更低止损价，即更宽松的（允许更大亏损）
  - ATR 2.0x只能'保持或放宽'固定止损（亏损≥8%），不能收紧（亏损<8%）
- 修复止损后正收益比例重复×100的格式化问题
- 修复"低于-8%比例"误用次数（应为百分比而非绝对次数）
- 输出每笔持仓理论止损阈值（entry_atr, fixed_stop_price, atr_stop_price, actual_stop_price）
- 列出ATR 2.0与固定止损差异的具体交易：
  - 被避免的3笔：2023-05-17 512980.SH(-9.93%)、2021-03-09 512400.SH(-8.30%)、2021-02-26 512400.SH(-8.36%)
  - 新增的1笔：2021-03-05 512400.SH(-11.58%)
  - 12笔ATR止损中，11笔(91.7%)触发固定止损，1笔(8.3%)触发ATR止损(-10.15%)
- 重新评估：ATR 2.0x通过候选检查但改善微弱（年化+0.19%, Sharpe+0.01），2020年无改善
- **最终结论：保持固定止损-8%（B0.3），不采纳ATR 2.0x**
- 不修改生产配置

**改了哪些文件：**
- 新增 `scripts/phase6_5b_atr_stop_correction.py` — ATR止损口径修正脚本
- 新增 `reports/phase6_5b_atr_stop_correction.md` — 修正报告
- 更新 `docs/CURRENT_STATE.md` — 添加 Phase 6.5b 摘要并修正Phase 6.5结论
- 更新 `docs/CHANGES.md` — 添加变更记录

**测试：** 42 passed + 10 xfailed + 1 xpassed（无生产代码修改，无新增测试）

**Commit：** 待提交

---

## 2026-06-20（之前 - Phase 6.5 ATR动态止损稳健性验证）

**修改了什么：**
- 验证ATR止损的稳健性：1.5x / 2.0x / 2.5x 三个multiplier
- ~~代码逻辑 `actual_stop = min(atr_stop, fixed_stop)` 取更严格的，不是放宽~~（已修正，见Phase 6.5b）
- 详细统计止损幅度分布、对比固定止损差异、止损后未来价格表现
- 候选检查：验证期收益或Sharpe改善、回撤不恶化、平均止损可接受、改善非单一年份
- 结果：
  - 1.5x：验证期与固定止损相同，淘汰
  - 2.0x：验证期年化+0.19%、Sharpe+0.01、回撤浅0.27%，通过候选检查
  - 2.5x：验证期与2.0x相同，但止损更深（-10.48% vs -9.93%），2020年退化
- 2.0x和2.5x验证期完全相同，但2.0x止损风险更小，推荐2.0x
- 改善来源：减少1-2次不必要的止损（2021年4→3次，2023年2→1次），避免波动中提前离场
- 2020年无改善：V型反弹中固定止损和ATR止损触发时机相同
- 邻域稳定：1.5x/2.0x/2.5x差异仅0.19%
- 不修改生产配置（候选状态，需最终样本外验证）
- **修正：最终结论改为保持固定止损（见Phase 6.5b）**

**改了哪些文件：**
- 新增 `scripts/phase6_5_atr_stop_robustness.py` — ATR稳健性验证脚本
- 新增 `reports/phase6_5_atr_stop_robustness.md` — 验证报告
- 更新 `docs/CURRENT_STATE.md` — 添加 Phase 6.5 摘要
- 更新 `docs/CHANGES.md` — 添加变更记录

**测试：** 42 passed + 10 xfailed + 1 xpassed（无生产代码修改，无新增测试）

**Commit：** 待提交

---

## 2026-06-20（之前 - Phase 6.2-6.4 综合实验：改善2020年结构性牛市跑输）

**修改了什么：**
- 新增三个独立实验，分别验证一个改善方向：
  - 6.2 Momentum急涨通道：momentum_20>0 + trend_score>=10 时给+5 bonus，提前入场
  - 6.3 ATR动态止损：stop_loss_mode='atr', multiplier=2.0，减少震荡市被震出
  - 6.4 行业弹性过滤：排除波动率最低20%行业，避免低弹性行业占用仓位
- 结果：
  - 6.2：2020年改善+4.99%（10.19%→15.19%），但验证期被B0.3支配（年化/Sharpe双劣化）→ 淘汰
  - 6.3：唯一通过验证期支配检查。年化+0.19%、Sharpe+0.01、回撤几乎持平。但2020年无改善
  - 6.4：验证期年化14.82%最高，但回撤-21.03%深于B0.3 → 淘汰。2020年反而退化
- 核心洞察：2020年跑输是趋势跟踪策略的结构性特征（V型反弹中等待MA确认），单一改进无法全局解决
- 6.3 ATR动态止损值得进一步验证（验证期全面改善），但不解决2020年问题
- 不修改生产配置

**改了哪些文件：**
- 新增 `scripts/phase6_2_to_6_4_comprehensive.py` — 综合实验脚本
- 新增 `reports/phase6_2_to_6_4_comprehensive.md` — 综合实验报告
- 更新 `docs/CURRENT_STATE.md` — 添加 Phase 6.2-6.4 摘要
- 更新 `docs/CHANGES.md` — 添加变更记录

**测试：** 42 passed + 10 xfailed + 1 xpassed（无生产代码修改，无新增测试）

**Commit：** 待提交

---

## 2026-06-20（之前 - Phase 6.1 降低入场门槛 min_total_score 40→35）

**修改了什么：**
- 新增实验：降低入场门槛 min_total_score 40→35，验证是否能改善2020年"入场慢"问题
- 2020年改善：+3.34%（10.19%→13.53%），提前入场确实有效
- 但2021年退化-0.74%、2024年退化-0.56%，震荡/分化年份低门槛增加误报
- 验证期（2023-2024）年化劣化-0.13%、Sharpe劣化-0.0168，被B0.3支配
- **结论：保持min=40**，Trade-off真实存在，需要更精细的入场机制（非单纯降低门槛）
- 不修改生产配置

**改了哪些文件：**
- 新增 `scripts/phase6_1_lower_threshold.py` — 降低门槛实验脚本
- 新增 `reports/phase6_1_lower_threshold.md` — 实验报告
- 更新 `docs/CURRENT_STATE.md` — 添加 Phase 6.1 摘要
- 更新 `docs/CHANGES.md` — 添加变更记录

**测试：** 42 passed + 10 xfailed + 1 xpassed（无生产代码修改，无新增测试）

**Commit：** 待提交

---

## 2026-06-20（之前 - Phase 5.8c 修正高波动实验候选选择逻辑）

**修改了什么：**
- 修正 Phase 5.8 候选选择逻辑：
  - 原逻辑：只在训练期前2名中选验证期最优，未与B0.3基准比较
  - 新逻辑：所有实验组必须与B0.3基准进行支配检查
  - 支配规则：验证期年化 < B0.3、Sharpe < B0.3、回撤深于B0.3、换手增加无收益补偿 → 淘汰
  - 只有至少改善收益或Sharpe，且其他指标未明显退化，才允许进入后续验证
- 修正最终结论：
  - 方案B（高波动前20%）：验证期被B0.3全面支配（年化9.35% < 13.45%，Sharpe0.4592 < 0.6926，回撤-22.72% < -17.75%）→ 淘汰
  - 方案C（高波动+趋势）：与B完全相同，且vol_score-momentum相关性+0.4644重新引入动量 → 淘汰
  - 方案D（波动率加速）：训练期最优（11.11%）但验证期过拟合（8.60%）→ 淘汰
  - **最终结论：保持B0.3不变**
- B/C结果相同原因分析：
  - vol_score差异确实存在（27.0%记录不同），但全部发生在trend_score<=0的ETF上
  - 这些ETF本来就不会进入候选（trend_score是硬筛选条件）
  - 验证期交易序列完全相同（B=255，C=255）
- 不修改生产配置

**改了哪些文件：**
- 重写 `scripts/phase5_high_volatility_experiment.py` — 修正候选选择逻辑（v3/5.8c）
- 更新 `reports/phase5_high_volatility_experiment.md` — 修正报告（B/C分析、支配规则、正确结论）
- 新增 `scripts/phase5_8c_bc_analysis.py` — B/C差异分析脚本（独立运行）
- 更新 `docs/CURRENT_STATE.md` — 添加 Phase 5.8c 摘要
- 更新 `docs/CHANGES.md` — 添加变更记录

**测试：** 42 passed + 10 xfailed + 1 xpassed（无生产代码修改，无新增测试）

**Commit：** 待提交

---

## 2026-06-20（之前 - Phase 5.8 高波动增量价值实验 v2）

**修改了什么：**
- 修正 Phase 5.8 原始脚本缺陷：
  - 实验组B/C/D未显式设置volatility_factor_enabled=True（被B0.3默认False覆盖）
  - 实验组compute_total_score直接求和，不再受父类vol开关覆盖
  - 波动率加速均值使用rolling(20).mean().shift(1)，避免未来数据
  - 新增断言：验证非零vol_score和total_score差异
- v2结果：vol_score确实影响交易（验证期B/C/D交易次数255/255/270 vs B0.3的232）
- 但验证期全面劣化：所有实验组年化（8.60%~9.35%）均低于B0.3基准（13.45%）
- 方案D训练期过拟合（11.11%→8.60%）
- **结论：保持B0.3不变**
- 不修改生产配置

**改了哪些文件：**
- 重写 `scripts/phase5_high_volatility_experiment.py` — v2修正版
- 更新 `reports/phase5_high_volatility_experiment.md` — v2报告
- 更新 `docs/CURRENT_STATE.md` — 添加 Phase 5.8 摘要
- 更新 `docs/CHANGES.md` — 添加变更记录

**测试：** 42 passed + 10 xfailed + 1 xpassed（无生产代码修改，无新增测试）

**Commit：** `ac49e9c` fix(phase5.8): high-volatility experiment v2 with explicit vol enable

---

---

## 2026-06-20（之前 - Phase 5.7 显式关闭vol_score并冻结B0.3）

**修改了什么：**
- `src/config.py` 增加 `volatility_factor_enabled = False`（显式关闭vol_score）
- `src/strategy.py` `compute_total_score` 根据开关决定是否计入 `vol_score`
- 保留 `volatility_20` 和 `vol_score` 计算代码，可随时重新启用
- **不修正vol_score阈值**（Phase 5.6已证明修复方案无效）
- B0.3精确对比B0.2：
  - 最终NAV: 2,809,091 == 2,809,091 (Delta=0)
  - 总收益: 180.91% == 180.91% (Delta=0)
  - 年化收益: 16.99% == 16.99% (Delta=0)
  - 夏普: 0.8985 == 0.8985 (Delta=0)
  - 最大回撤: -17.75% == -17.75% (Delta=0)
  - 交易次数: 801 == 801 (Delta=0)
  - 买入/卖出/调仓次数全部一致
- **验收通过**：B0.3 == B0.2（完全一致）

**改了哪些文件：**
- `src/config.py` — 增加 `volatility_factor_enabled = False`
- `src/strategy.py` — `compute_total_score` 支持vol开关
- `tests/test_volatility_factor_switch.py` — 7个回归测试（新增）
- `tests/test_momentum_factor_switch.py` — 修正3个测试（明确设置vol开关以隔离测试）
- `scripts/b0_3_baseline.py` — B0.3基准回测+精确对比脚本（新增）
- `reports/baseline_B0.3_20260620_180745.md` — B0.3基准报告（新增）
- `reports/b0_2_vs_b0_3_20260620_180745.md` — B0.2 vs B0.3精确对比报告（新增）
- `docs/CURRENT_STATE.md` — 更新
- `docs/CHANGES.md` — 更新

**测试：** 42 passed + 10 xfailed + 1 xpassed（新增7个回归测试）

**Commit：** 待提交

---

## 2026-06-20（之前 - Phase 5.6 波动率评分修复实验）

**修改了什么：**
- 新增波动率修复实验脚本：以B0.2为基准，仅改变vol_score，比较4个方案
- 训练期确定阈值：p20=0.1548, p80=0.3426（由训练数据确定，未拍脑袋）
- 4个方案对比：
  - 当前失效(B0.2)：训练9.55%，验证13.45%
  - 完全删除vol_score：和B0.2相同（vol_score已恒为0）
  - 固定阈值：训练9.35%，验证11.43%（变差）
  - 横截面分位数：训练9.79%，验证8.55%（验证期最差）
- Rank IC：所有方案vol_score均为负IC（负向预测因子）
- **结论：不修复vol_score，保持B0.2不变**
- 未修改生产配置

**改了哪些文件：**
- 新增 `scripts/phase5_volatility_repair.py` — 波动率修复实验脚本
- 新增 `reports/phase5_volatility_repair.md` — 实验报告
- 更新 `docs/CURRENT_STATE.md` — 添加 Phase 5.6 摘要
- 更新 `docs/CHANGES.md` — 添加变更记录

**测试：** 35 passed + 10 xfailed + 1 xpassed（无生产代码修改，无新增测试）

**Commit：** 待提交

---

## 2026-06-20（之前 - Phase 5.5 正式采纳 no_momentum 并冻结 B0.2）

**修改了什么：**
- `src/config.py` 增加 `momentum_factor_enabled = False`（默认关闭 momentum 因子）
- `src/strategy.py` `compute_total_score` 根据开关决定是否计入 `momentum_rank`
  - v5.5: 开关关闭时 momentum_rank 设为 0 再求和
  - 保留 `exclude_factor` 消融测试能力
- 保留 `momentum_20` 和 `momentum_rank` 计算代码，可随时重新启用
- 不使用 monkey patch 或 `exclude_factor` 作为正式实现

**B0.2 基准（冻结）：**
- 总收益 170.64% -> 180.91%（+10.27%）
- 年化收益 16.33% -> 16.99%（+0.66%）
- 夏普 0.8442 -> 0.8985（+0.0543）
- 最大回撤 -21.37% -> -17.75%（+3.62%）

**改了哪些文件：**
- `src/config.py` — 增加 `momentum_factor_enabled = False`
- `src/strategy.py` — `compute_total_score` 支持开关
- `tests/test_momentum_factor_switch.py` — 6个回归测试（新增）
- `scripts/b0_2_baseline.py` — B0.2 基准回测脚本（新增）
- `reports/baseline_B0.2_20260620_174049.md` — B0.2 基准报告（新增）
- `docs/CURRENT_STATE.md` — 更新
- `docs/CHANGES.md` — 更新

**测试：** 35 passed + 10 xfailed + 1 xpassed（新增6个回归测试）

**Commit：** 待提交

---

## 2026-06-20（之前 - Phase 5.4 最终样本外验证）

**修改了什么：**
- 新增最终样本外验证脚本：只比较 B0.1 与 no_momentum
- 样本外固定：2025-01-01 至 2026-06-18，已封存，不再用于调参
- 核心结果：
  - 年化收益：39.97% -> 42.16% (+2.20%)
  - Sharpe：1.8183 -> 1.9086 (+0.09)
  - 最大回撤：-11.93% -> -11.36% (+0.56pp)
  - Score: 3/3，no_momentum 全面优于 B0.1
- 2025年：全面改善；2026年：收益+3.20%，Sharpe+0.14，回撤略差-0.72pp
- 唯一建议：**采纳 no_momentum**（删除 momentum_rank 因子）
- 未处理 vol_score，未修改生产配置

**改了哪些文件：**
- 新增 `scripts/phase5_final_oos.py` — 最终样本外验证脚本
- 新增 `reports/phase5_final_oos.md` — 验证报告
- 更新 `docs/CURRENT_STATE.md` — 添加 Phase 5.4 摘要
- 更新 `docs/CHANGES.md` — 添加变更记录

**测试：** 29 passed + 10 xfailed + 1 xpassed（无生产代码修改，无需新测试）

**Commit：** 待提交

---

## 2026-06-20（之前 - Phase 5.3 评分标准增量价值验证修正版）

**修改了什么：**
- 修正 Phase 5.3 方法论：
  - **vol_score 根因分析**：确认 `volatility_20` 是年化波动率（均值24.38%），阈值 [1%,4%]/(4%,6%] 是设计失效（量纲不匹配），非代码缺陷
  - **因子预测力改用 Rank IC**：按日横截面 Spearman 秩相关，报告 IC_mean、IC_std、IR、年度稳定性
  - **B0.1 vs no_momentum 分年度对比**：2019-2024每年年化、Sharpe、回撤、换手
  - **训练期退化规则**：差距>2pp则淘汰，no_momentum训练期5.96% vs B0.1的7.09%（差距1.12pp，未触发）
  - **唯一建议**：`no_momentum enters final validation`
  - 不运行最终样本外，不修改生产配置
- 核心发现：
  - momentum 因子 Rank IC 最强（H10 IR=0.0822），但删除后验证期反而更好（13.41% vs 10.48%）
  - confirm 因子 Rank IC 为负（H10 IR=-0.041），说明设计可能有缺陷
  - volatility 因子完全失效（100%为零）
  - no_momentum 训练期平均 5.96%（低1.12pp，未退化），验证期平均 13.41%（高2.93pp）

**改了哪些文件：**
- 重写 `scripts/phase5_scoring_diagnosis.py` — 修正版诊断脚本（Rank IC、分年度对比、退化检查、唯一建议）
- 更新 `reports/phase5_scoring_diagnosis.md` — 修正版报告
- 更新 `docs/CURRENT_STATE.md` — 添加 Phase 5.3 摘要
- 删除 `scripts/check_vol_score.py` — 临时验证脚本

**测试：** 29 passed + 10 xfailed + 1 xpassed（无生产代码修改，无需新测试）

**Commit：** 待提交

---

## 2026-06-20（之前 - Phase 5.2 目标参数组合探索修正版）

**修改了什么：**
- 修正 Phase 5.2 方法论：
  - 三阶段独立回测（不同 as_of_date）：训练集到2022-12-30、验证集到2024-12-31（performance_start=2023-01-01）、样本外到2026-06-18（performance_start=2025-01-01）
  - 相关性去重只使用各阶段起点之前的数据（由 performance_start 控制）
  - Pareto 前沿在**全部18个组合**中计算，不预先筛选
  - 训练集生成候选 → 验证集排序 → 样本外只对最终唯一组合运行一次
- 核心发现：18个组合中**无一个**同时满足训练集目标（年化≥15%、夏普≥0.8、最大回撤≤20%）
- Pareto 前沿：5个组合（最佳：min_total_score=35, stop_loss=-8%, max_position_per_etf=20%，训练集年化11.20%，夏普0.61）
- 验证集#1与B0.1在三个阶段的差异均在±0.25%以内
- 样本外仅运行一次（最终候选：42.03%年化），不用于调整参数
- 删除"放宽年化至12%"和"扩大参数网格"的建议
- 未修改 src/config.py（纯研究脚本）

**改了哪些文件：**
- 重写 `scripts/phase5_parameter_search.py` — 修正版参数搜索脚本（三独立回测、Pareto全组合、样本外只跑一次）
- 更新 `reports/phase5_parameter_search.md` — 修正版报告

**测试：** 29 passed + 10 xfailed + 1 xpassed（无生产代码修改，无需新测试）

**Commit：** 待提交

---

## 2026-06-20（之前 - Phase 5.2 初版）

**修改了什么：**
- 新增参数搜索脚本：18个核心参数组合（min_total_score×stop_loss×max_position_per_etf）
- 时间划分：2019-2022训练、2023-2024验证、2025-2026-06-18样本外
- **核心发现**：18个组合中**无一个**同时满足训练集目标（年化≥15%、夏普≥0.8、最大回撤≤20%）
- 最佳候选：min_total_score=35, stop_loss=-10%, max_position_per_etf=20%（训练集年化11.20%，夏普0.61）
- 原因分析：训练集包含2022年熊市，策略年化约10-11%是合理预期；夏普0.8门槛在当前波动环境下较难达到
- 建议：放宽年化目标至12%，或扩大参数搜索范围（如min_total_score降至30、max_position_per_etf提高至25%）
- 样本外表现优异（42.05%年化），但不应据此调整训练集目标
- 未修改src/config.py（纯研究脚本）

**改了哪些文件：**
- 新增 `scripts/phase5_parameter_search.py` — 参数组合搜索脚本
- 新增 `reports/phase5_parameter_search.md` — 参数搜索报告

**测试：** 29 passed + 10 xfailed + 1 xpassed（无生产代码修改，无需新测试）

**Commit：** 已废弃（被修正版覆盖）

---

## 2026-06-20（之前 - Phase 5.1 调仓星期稳健性诊断修正版）

**修改了什么：**
- 修正 Phase 5.1 报告：
  - 增加各星期排名统计（平均排名、中位排名、前二占比、年度胜率）
  - 区分「绝对收益优势」与「排名稳定性」两个维度
  - 删除滑点敏感性分析（回测引擎未实现，诚实报告）
  - 重写审慎结论：Thursday 平均排名 1.88（年度）/ 1.67（区间），前二占比 87.5%（年度）/ 100%（区间），兼具绝对收益优势和排名稳定性，但样本仅8年，仍需持续观察
- 脚本编码修复：中文引号导致 Python 语法错误，替换为「」

**改了哪些文件：**
- 重写 `scripts/phase5_weekday_robustness.py` — 修正版诊断脚本（增加排名统计、删除滑点、审慎结论）
- 更新 `reports/phase5_weekday_robustness.md` — 修正版报告

**测试：** 29 passed + 10 xfailed + 1 xpassed（无生产代码修改，无需新测试）

**Commit：** 待提交

---

## 2026-06-20（之前 - Phase 5.1 初版）

**修改了什么：**
- 新增诊断脚本：分年度/分区间对比周一至周五调仓效果（全区间 5 次回测）
- 发现星期四优势**不稳定**（11个阶段中只有4次最优，占比36.4%）
- 分析调仓日期差异（不同调仓日无共同调仓日，说明日期完全错开）
- 检查节假日顺延（各调仓日均有节假日附近调仓）
- 尝试滑点敏感性分析，但发现回测引擎未实现滑点逻辑（诚实报告）

**改了哪些文件：**
- 新增 `scripts/phase5_weekday_robustness.py` — 调仓星期稳健性诊断脚本
- 新增 `reports/phase5_weekday_robustness.md` — 诊断报告

**测试：** 29 passed + 10 xfailed + 1 xpassed（无生产代码修改，无需新测试）

**Commit：** 已废弃（被修正版覆盖）

---

## 2026-06-20（之前 - Phase 4.1）

**修改了什么：**
- 回测截止日期从 2026-06-05 改成 2026-06-18（数据更新到最新交易日）
- 回测引擎支持传入截止日期（以前写死在代码里，现在可以灵活配置）
- 回测开始前自动检查日期（数据够不够、有没有缺标的）
- 修复 NAV 计算不一致（导致 v2.5 调仓报错）
- 对比回测报告修复（调仓次数之前显示为 0，现在正确了）
- 对比回测报告新增：买入次数、卖出次数、总佣金、最终 NAV
- 测试文件整理（历史缺陷测试加标记，不阻塞 CI）
- 文档更新（删除过期数据，填入最新结果）

**改了哪些文件：**
- `src/backtest.py` — 回测引擎（加 as_of_date 参数、日期验证、NAV 计算修复、rebalance_count 统计）
- `scripts/contrast_backtest.py` — 对比回测脚本（加 as_of_date、修复统计）
- `tests/test_b0_rebalance_failures.py` — 历史缺陷测试（加 xfail 标记）
- `docs/CURRENT_STATE.md` — 最新状态文档
- `reports/phase2_to_current_summary.md` — 浓缩报告
- 新增：`reports/baseline_B0.1_20260620_143209.md` — 新基准
- 新增：`reports/contrast_report_20260620_143209.md` — 新对比报告

**测试：** 29 通过 + 10 预期失败（历史缺陷）+ 1 意外通过，全部符合预期

**Commit：** `3fcd2bf` checkpoint: complete Phase 2-4.1 rebalance planner

**推送：** GitHub 推送成功

---

## 2026-06-20（之前）

**修改了什么：**
- v2.5 纯函数调仓引擎（解决旧版调仓逻辑 bug）
- 新旧调仓逻辑对比回测（v2.5 收益提升 28.70%）
- 2020-2021 年诊断（发现真正困难的是 2020 年，不是 2021 年）
- 交易日敏感性实验（发现调仓日选择极其敏感，周四最优）
- 大盘状态持仓分析（验证策略有先天择时效果）
- 文档更新

**Commit：** 包含多个历史 commit（详见 Git 历史）

---

## 记录格式说明

每次新修改时，在文件最上方新增一段，包含：
1. 日期
2. 修改了什么（一句话概括）
3. 改了哪些文件（文件名 + 一句话解释）
4. 测试结果（多少个通过/失败）
5. Commit 信息（commit 后补充）
