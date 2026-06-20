# 项目记忆层设计

## 目标

让 Codex、Kimi 或新对话无需读取完整历史聊天和大型同步日志，也能准确恢复当前工程现场。

## 结构

- `AGENTS.md`：长期有效的协作规则、验证要求和新线程启动顺序。
- `docs/CURRENT_STATE.md`：唯一的当前现场，允许覆盖更新，保持短小。
- `docs/DECISIONS.md`：已经确认的长期决策，只追加或明确废止，不记录过程流水账。
- `KIMI_CODEX_SYNC.md`：继续保留为历史协作日志，但不再作为新线程首要入口。
- `HANDOFF.md`：保留旧文件，不删除；以 `docs/CURRENT_STATE.md` 为准。

## 更新规则

1. 每个阶段验收、方向改变或对话交接前更新 `docs/CURRENT_STATE.md`。
2. 只有经过用户确认或可靠验收的长期结论才写入 `docs/DECISIONS.md`。
3. 研究过程、长日志和逐轮结果继续进入报告或同步日志，不复制进当前状态。
4. `docs/CURRENT_STATE.md` 目标不超过 300 行；如果增长，归档旧内容而不是继续堆叠。
5. 新线程先读三个入口文件和 `git status`，仅在当前任务需要时读取历史报告。

## 恢复成功标准

新线程只读取：

1. `AGENTS.md`
2. `docs/CURRENT_STATE.md`
3. `docs/DECISIONS.md`
4. `git status --short --branch`

即可回答：

- 当前分支、阶段和唯一目标是什么；
- 哪些结论已冻结；
- 哪些代码尚未验收；
- 当前阻塞问题和下一步是什么；
- 哪些文件或规则不得擅自修改。

