# 当前工程现场

**最后更新**：2026-10-21
**工作目录**：`D:\etf_rotation_model`
**当前分支**：`feature/paper-trading-ui-v0.3`（HEAD `5308015`，已推送远端）
**当前版本**：v1.2.3（虚拟盘 v0.3.1）
**发布锚点**：v1.2.3-b0.4 → 5e8eb78（标签保持不动）
**正式基线**：B0.4（见 `docs/B0_BASELINE_LOCK.md`）

### 数据截止日（两个口径，勿混淆）

| 口径 | 截止日 | 说明 |
|------|--------|------|
| **B0.4 冻结数据** | **2026-06-18** | 冻结段 ≤2026-06-18，快照 `data/snapshots/B0_4_candidate_data_20260621_210815.csv`，SHA-256 前缀 dbdb041d（全量 e0cf2993…），全轮未动 |
| **当前数据库行情** | **2026-10-20** | market_data 最新日期（2026-10-21 查询确认，112,321 行） |
| **当前评分** | **2026-10-20** | daily_scores 最新日期（同日查询确认） |

corporate_actions 表 9 条拆分记录全部 confirmed（含公告依据）。旧分支 `feature/v1.2.1-regime-adaptive` 保留不动。

---

## 1. 当前结论（最终状态）

- **真实价格架构 v3 + 拆分安全修复：已验收收口**（提交 `5308015`，Trae ROUND4/5/6 全部关闭）——本节第 3 条详述
- **B1 Holding Stability A/B：已终审收口，不升级 B1**——A/B/C 夏普均低于 B0.4 的 0.91 且回撤恶化；卖出缓冲减少换仓有效但风险调整后变差
- **Universe Time-Consistency Audit：已收口**——7 只 ETF 早期覆盖不足已量化披露，四组对照不足以推翻 B0.4
- **Paper Trading v0.3.1：已完成**——账户生命周期（结束/隐藏/软删除/永久删除级联）21 项测试覆盖，浏览器验收通过
- **实盘助手 v0.2：骨架已交付**（持仓/止损/调仓建议/成交记录，不自动下单，22 项测试）
- **v1.3 Step 1~10：已完成，全部 observer-only**——无任何一步达到升级 B0.4 的预注册标准；B0.4 保留
- **行情更新链路：已修复可用**——AKShare 超时治理 + iFinD CSV 自动导入 fallback + iFinD 双序列拉取

历史细节见 `docs/CHANGES.md`。

---

## 2. B0.4 冻结口径

- 18 只 ETF（16 行业 + 黄金 518880 + 国债 511010）；每周四调仓；最多 5 只、单只新建仓上限 20%；固定止损 -8%
- 动量/波动率因子关闭；市场状态模块不参与交易；佣金双向 0.03% 最低 5 元、100 股整手、不计滑点
- 执行口径：信息日期 = 前一交易日收盘，成交 = 次日开盘；关键指标用 `shift(1)`，已过同日扰动测试

### 冻结指标（复现见 `tests/test_b0_4_slippage.py`，0bp = 冻结快照）

| 指标 | B0.4 |
|------|------|
| 最终 NAV | 2,761,288.07 |
| 总收益 | 176.13% |
| 交易次数 | 804 |

---

## 3. 本轮修复最终状态（2026-10-20 ~ 10-21，已验收）

**价格双轨制**：market_data 存 iFinD 真实 OHLCV（adjust_type='real_spliced'）+ 校准 adj_close；评分管线经 `apply_adjustment_for_scoring` 用调整价（价 ×adj/close、量 ÷adj/close，无 adj_close 恒等 = 冻结路径零变化）；成交/估值用真实价，股数与佣金按真实市场计算；volume 恒为真实股数不乘换算因子。

**已确认拆分处理**：corporate_actions 登记 9 条真实拆分（status/evidence 列，`confirm_corporate_actions` 为唯一确认入口）。回测引擎 `run()` 支持 corporate_actions（缺省 None），盘前 shares×=ratio、cost/high_water/atr÷=ratio，修复了 512480 在 2026-07-03 被误读为 -51% 错误止损、组合虚假下跌 9.9% 的问题。虚拟盘 runner 盘前同语义调整，市值不变、不误止损。

**未确认拆分保护**：
- pending 涉持仓（含 confirmed 但 ratio 非法）→ 回测抛 `PendingCorporateActionError` 整体暂停；虚拟盘 paused：不交易、不估值、不保存当日状态，确认后重跑该日恢复
- pending 未持仓 → 该标的新买入资格持续暂停直到确认
- **跳期防护**：已确认拆分生效日未处理时，禁止直接续跑后续日期（明确提示先补跑生效日），不能带着旧股数恢复
- 页面查询（`app._load_corporate_actions`）返回生效日 ≤ 当日的全部记录（历史 confirmed 不丢）+ 全部 pending
- **公司行动查询失败 → 停止本次运行并明确报错**，绝不当作无记录继续

**基准收益口径统一**：图表/年度表/区间卡全部按各自比较区间 bench_price 首末比 - 1（`bench_return_in_range`）；年度表旧差分算法确认有误，已废弃。

**迁移执行结果**（正式库，已验证）：删旧等效拼接行 1389 → 双序列重生成 1408 行 → 全历史 adj_close 重算 29,530 行 → 评分重算 33,895 条；B0.4 冻结段与修复前备份逐行比对 0 差异；拆分边界 adj 收益率平滑（512480 7/2→7/3 仅 -1.41%）；无 forward_spliced 残留。备份：`database/backups/etf_model_20261020_172326_pre_real_price_v3.db`。

**遗留（如实）**：AKShare fallback 路径未真实调用（未独立验证）；512480 的 6/22~6/29 旧数据与核验来源不一致，**原因未确认**（不作"盘中快照"定性）。

---

## 4. 验证范围（如实记录，不夸大）

**本会话（Kimi）实测**：
- `tests/test_real_price_adjustment.py` 20 passed、回测拆分 10 + 基准口径 5 passed、data_fetcher 15 passed（提交前合计 50 passed）
- 全量回归 412 passed / **2 failed**——失败为基线既有问题：`test_missing_data_antipattern`、`test_v1_3_step6_dynamic_fifth_slot`（regime 分布）；证据：在 HEAD 干净副本（git worktree）可复现，**原因未确认**，与本轮改动无关
- CLI 全样本回测正常完成（总收益 140.99%、927 笔、拆分日 0 误止损）

**Codex 独立验证（非本会话重跑）**：
- B0.4 冻结基线复现：NAV 2,761,288.07、804 笔交易、冻结快照 SHA-256 不变
- 恢复流程：暂停 → 确认 → 补跑 → 正常恢复

早前各轮结果（388 passed 等）未在本次全部重跑，仅作历史记录。

---

## 5. 工程状态备忘

**Paper Trading 资金勾稽（真实测试保证）**：现金+持仓市值=总资产（每日验证）；佣金计入现金；买入不得负现金；卖出不得超持仓。已知限制：未定时自动运行、不连接券商。

**禁止修改范围**：B0.4 ETF 池/评分因子/阈值、动量与波动率开关冻结状态、调仓日/最大持仓/单只上限/止损及防御规则、规划器 v2.5 业务语义、冻结快照与 SHA-256、`docs/B0_BASELINE_LOCK.md`、已封存样本外结果。所有增强必须独立开关、以 B0.4 为对照、一次只变一个变量。

**版本口径**：策略 v1.2.3（B0.4 基线）/ 引擎 v2.5 / 数据准入 v1.1 / 滑点测试 v2 / 虚拟盘 v0.3.1。

---

## 6. 下一步唯一任务

1. **修正 8 月复盘数字与解释**——`reports/august_loss_review.md` 中三笔大亏的归因需复核（已发现入场 momentum_rank 全为 0，系 v1.2.2 市场质量门清零副作用，复盘数字与解释需相应修正）
2. 然后**开展历史买入点诊断**
3. **暂不增加交易规则**

---

## 7. 新线程恢复方式

依次读取：`AGENTS.md` → `docs/CURRENT_STATE.md` → `docs/DECISIONS.md` → `git status --short --branch`
