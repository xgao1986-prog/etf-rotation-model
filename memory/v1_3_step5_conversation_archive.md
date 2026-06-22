# v1.3 Step 5 对话存档（2026-06-24）

> 用途：供新对话快速恢复上下文。由用户主动要求存档。

## 当前状态

- **HEAD**: `15fe083` on `feature/v1.3-regime-research`
- **数据截止**: 2026-06-18
- **基线**: B0.4 (NAV=2,761,288.07, 交易804笔)

## Step 5 已完成

- **脚本**: `scripts/v1_3_step5_dynamic_breadth_diagnosis.py`
- **报告**: `reports/v1_3_step5_dynamic_breadth_diagnosis.md`
- **CSV 数据**（被 gitignore 排除，需本地运行脚本重新生成）：
  - `v1_3_step5_rebalance_events.csv`（271行）
  - `v1_3_step5_fifth_candidate_events.csv`（82行）
  - `v1_3_step5_concentration_counterfactual.csv`（54行）
  - `v1_3_step5_summary.csv`

## 核心结论（已写入 CURRENT_STATE.md / CHANGES.md）

- **第5名价值**: 完整20日观察81笔，平均收益+1.54%，但分组（候选数量/质量/分差）无稳定规律。
- **3-4只候选集中反事实**: 数学上的收益-风险交换，未经实际交易验证。
- **研究期/验证期方向**: 均为正（0.98% vs 1.22%），但验证期仅18笔。
- **预注册**: 7项标准未全部通过。
- **结论**: 当前证据不足，继续使用固定B0.4结构，不制定动态规则。

## 本轮对话中的一个错误（需记住）

- **B0.4 的 stock_max_holdings=5，它确实可以持有 5 只行业 ETF**（当候选数量≥5时）。
- 我在本轮对话中错误地说"B0.4从未持有第5名"——这是幻觉，与 config 和 nav_df 数据均矛盾。
- nav_df 显示：持仓5只的天数=759天，是全期最频繁状态。
- 方案B（Step 4实验）是 stock_max_holdings=4，它才不持有第5名。
- **不要混淆 B0.4 和方案B 的持仓数量。**

## 新对话恢复建议

1. 读 `docs/CURRENT_STATE.md` 获取当前工程状态。
2. 读 `reports/v1_3_step5_dynamic_breadth_diagnosis.md` 获取 Step 5 完整报告。
3. 如需重新生成 CSV，运行 `scripts/v1_3_step5_dynamic_breadth_diagnosis.py`。
4. 注意：Step 5 结论是"证据不足，不制定动态规则"，不是"发现有效信号"。
