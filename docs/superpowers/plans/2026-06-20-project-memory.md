# Project Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立一个小型、可持续维护的仓库内项目记忆层，降低长对话上下文耗尽造成的恢复成本。

**Architecture:** 长期协作规则、当前现场和稳定决策分别存放，避免大型同步日志同时承担三种职责。新线程使用固定的四步只读启动流程恢复上下文。

**Tech Stack:** Markdown、Git、现有项目文档。

---

### Task 1: 建立长期协作规则

**Files:**
- Create: `AGENTS.md`

- [x] **Step 1: 定义新线程读取顺序**
- [x] **Step 2: 定义验证、单变量研究和工作区保护规则**
- [x] **Step 3: 定义长输出与交接控制规则**

### Task 2: 建立唯一当前现场

**Files:**
- Create: `docs/CURRENT_STATE.md`

- [x] **Step 1: 记录当前分支、提交和工作区风险**
- [x] **Step 2: 记录当前调仓引擎 v2.2 审查结果**
- [x] **Step 3: 记录下一步唯一任务和禁止扩大范围**

### Task 3: 建立稳定决策登记

**Files:**
- Create: `docs/DECISIONS.md`

- [x] **Step 1: 记录版本边界和研究纪律**
- [x] **Step 2: 记录调仓引擎已确认的业务语义**
- [x] **Step 3: 记录已淘汰实验和证据状态**

### Task 4: 验证恢复入口

**Files:**
- Verify: `AGENTS.md`
- Verify: `docs/CURRENT_STATE.md`
- Verify: `docs/DECISIONS.md`

- [x] **Step 1: 搜索占位符和自相矛盾内容**

Run:

```powershell
Select-String -Path AGENTS.md,docs\CURRENT_STATE.md,docs\DECISIONS.md -Pattern 'TBD|TODO|待补充'
```

Expected: no matches.

- [x] **Step 2: 检查文档规模和Git变更**

Run:

```powershell
Get-Content AGENTS.md,docs\CURRENT_STATE.md,docs\DECISIONS.md | Measure-Object -Line
git status --short -- AGENTS.md docs
```

Expected: 三个入口文件存在，当前状态保持精简，只有预期新增文档。
