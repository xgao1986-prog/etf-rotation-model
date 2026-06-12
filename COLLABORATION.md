# 🤝 Kimi × Codex 协作手册

> 本文档定义Kimi（Moonshot AI）与Codex（OpenAI）在ETF轮动项目中的协作规则。
> 任何一方开始工作前，必须先阅读本文档和 `HANDOFF.md`。

---

## 一、角色分工

### 🧠 Kimi（策略大脑）

| 职责 | 说明 |
|------|------|
| **策略设计** | 评分体系、入场/出场条件、风控规则 |
| **数据分析** | 回测结果解读、参数敏感性分析、问题诊断 |
| **数据获取** | 通过Kimi内置工具获取iFinD数据，写入本地数据库 |
| **用户沟通** | 理解用户需求，转化为技术任务 |
| **文档维护** | 更新 `PROGRESS.md`、`HANDOFF.md`、策略说明 |

**Kimi不直接做的：**
- ❌ 大规模代码重构（交给Codex）
- ❌ GitHub操作（交给Codex）
- ❌ 复杂测试用例编写（交给Codex）

### 💻 Codex（工程实现）

| 职责 | 说明 |
|------|------|
| **代码实现** | 将Kimi的策略设计转化为代码 |
| **代码重构** | 优化代码结构、性能、可读性 |
| **GitHub同步** | 推送到GitHub仓库，管理PR/Issue |
| **测试覆盖** | 编写单元测试、集成测试 |
| **CI/CD** | GitHub Actions自动化测试、部署 |
| **环境管理** | 依赖管理、Docker配置、部署脚本 |

**Codex不直接做的：**
- ❌ 修改策略逻辑（需Kimi确认）
- ❌ 修改数据库中的历史数据（需Kimi确认）
- ❌ 更改策略参数权重（需Kimi确认）

---

## 二、协作原则

### 原则1：先读接力日志，再动手

每次开始工作前，必须：
1. 阅读 `HANDOFF.md` 了解当前状态
2. 确认没有未完成的交接
3. 更新 `HANDOFF.md` 标记自己开始工作

### 原则2：文件锁定机制

**锁定方式：** 在 `HANDOFF.md` 的 "当前工作" 区域声明

```markdown
## 当前工作

| 协作者 | 正在修改的文件 | 预计完成时间 | 备注 |
|--------|---------------|-------------|------|
| Kimi | `src/strategy.py` | 2025-06-12 14:00 | 调整评分权重 |
```

**规则：**
- 一方锁定文件后，另一方不得修改该文件
- 如需修改对方锁定的文件，先在 `HANDOFF.md` 中请求交接
- 完成工作后，立即解锁并更新 `HANDOFF.md`

### 原则3：Git分支隔离

```
main                    # 稳定分支，只有经过验证的代码
├── feature/kimi-xxx    # Kimi的策略实验分支
├── feature/codex-xxx   # Codex的工程实现分支
└── hotfix/xxx          # 紧急修复分支
```

**规则：**
- Kimi的策略修改在 `feature/kimi-xxx` 分支
- Codex的工程修改在 `feature/codex-xxx` 分支
- 合并到 `main` 前，另一方必须Review
- Codex负责GitHub上的分支管理

### 原则4：策略变更需双方确认

以下变更必须双方同意：
- 评分权重调整
- 入场/出场条件修改
- 新增/删除ETF标的
- 数据源切换
- 回测区间调整

**确认方式：** 在 `HANDOFF.md` 中记录讨论结果

---

## 三、工作流

### 场景A：Kimi发现策略问题，需要Codex修复代码

```
1. Kimi在HANDOFF.md中记录问题诊断
2. Kimi锁定相关文件，进行策略调整
3. Kimi完成策略调整，更新HANDOFF.md
4. Kimi通知Codex（通过用户转达或HANDOFF.md标记）
5. Codex读取HANDOFF.md，理解需求
6. Codex创建feature分支，实现代码
7. Codex提交PR，请求Kimi Review
8. KimiReview通过，合并到main
```

### 场景B：Codex发现代码Bug，需要修复

```
1. Codex在HANDOFF.md中记录Bug描述
2. 如果是策略相关Bug（如未来数据泄露），通知Kimi确认修复方案
3. 如果是纯代码Bug（如类型错误），Codex直接修复
4. Codex提交PR，简要说明修复内容
5. 如影响策略逻辑，等待Kimi确认后合并
```

### 场景C：用户提出新需求

```
1. Kimi与用户讨论需求，转化为技术任务
2. Kimi在HANDOFF.md中记录需求分解
3. 策略部分由Kimi负责，工程部分由Codex负责
4. 双方各自在feature分支工作
5. 完成后合并到main
```

---

## 四、文件所有权

| 文件/目录 | 主要维护者 | 修改需通知 |
|-----------|-----------|-----------|
| `src/strategy.py` | Kimi | Codex |
| `src/backtest.py` | Kimi | Codex |
| `src/config.py` | 双方 | 双方 |
| `src/database.py` | Codex | Kimi |
| `src/data_fetcher.py` | Codex | Kimi |
| `main.py` | Codex | Kimi |
| `app.py` | Codex | Kimi |
| `requirements.txt` | Codex | - |
| `README.md` | 双方 | - |
| `PROGRESS.md` | Kimi | Codex |
| `HANDOFF.md` | 双方 | - |
| `COLLABORATION.md` | 双方 | - |
| `database/etf_model.db` | Kimi | Codex |
| `reports/` | Kimi | - |
| `signals/` | Kimi | - |

---

## 五、代码规范

### 提交信息格式

```
[Kimi/Codex] 类型: 简要描述

详细说明（可选）

相关: #Issue编号
```

**类型：**
- `feat`: 新功能
- `fix`: Bug修复
- `strategy`: 策略调整
- `refactor`: 重构
- `docs`: 文档
- `test`: 测试
- `data`: 数据更新

**示例：**
```
[Kimi] strategy: 将动量权重从25%降至20%

回测显示2020-2021年动量因子失效，降低权重以改善回撤。

相关: #12
```

### 代码注释标记

```python
# [Kimi] 策略逻辑，修改需确认
# [Codex] 工程实现，可自由优化
# [TODO:Kimi] Kimi待处理
# [TODO:Codex] Codex待处理
# [HACK] 临时方案，需后续优化
```

---

## 六、冲突解决

### 场景1：双方同时修改同一文件

1. 先完成的一方提交并更新 `HANDOFF.md`
2. 后完成的一方必须先 `git pull` 获取最新代码
3. 手动解决冲突，优先保留策略逻辑（Kimi的修改）
4. 如无法判断，在 `HANDOFF.md` 中标记，等待用户裁决

### 场景2：策略意见分歧

1. 双方在 `HANDOFF.md` 中各自阐述观点
2. 提供回测数据支持
3. 如无法达成一致，由用户最终决策
4. 记录决策理由，避免未来重复争论

---

## 七、GitHub同步（Codex负责）

### 仓库设置

```bash
# Codex在GitHub创建仓库
git remote add origin https://github.com/用户名/etf-rotation-model.git

# 推送main分支
git push -u origin main
```

### 分支保护规则

- `main` 分支：需要PR Review，不能直接推送
- `feature/*` 分支：自由推送
- 合并到 `main` 前必须通过CI测试

### GitHub Actions（Codex配置）

```yaml
# .github/workflows/ci.yml
name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.10'
      - name: Install dependencies
        run: pip install -r requirements.txt
      - name: Run tests
        run: pytest tests/
```

---

## 八、沟通协议

### 方式1：通过文件（推荐，有记录）

在 `HANDOFF.md` 中留言：

```markdown
## 留言板

### 2025-06-12 Kimi → Codex
@Codex: 请帮忙把 `src/strategy.py` 中的 `calculate_scores` 方法拆分成
5个独立方法（每个评分维度一个），方便后续调参。我已经在
`feature/kimi-refactor` 分支上锁定了这个文件，完成后我会解锁。

### 2025-06-12 Codex → Kimi
@Kimi: 已拆分完成，见 `feature/codex-refactor` 分支。请Review后合并。
```

### 方式2：通过用户转达

用户作为中间人传递信息：
- 用户："Kimi说策略权重需要调整"
- 用户："Codex说GitHub仓库已创建"

### 方式3：GitHub Issue/PR评论

在GitHub上通过Issue和PR评论讨论技术细节。

---

## 九、快速参考

### Kimi开始工作

```bash
# 1. 读取状态
cat HANDOFF.md

# 2. 确认无冲突后，标记开始工作
# （编辑HANDOFF.md，添加当前工作记录）

# 3. 创建/切换到feature分支
git checkout -b feature/kimi-$(date +%Y%m%d)

# 4. 工作...

# 5. 提交并更新HANDOFF.md
git add -A
git commit -m "[Kimi] 类型: 描述"
# 更新HANDOFF.md，标记完成
```

### Codex开始工作

```bash
# 1. 读取状态
cat HANDOFF.md

# 2. 确认无冲突后，标记开始工作
# （编辑HANDOFF.md，添加当前工作记录）

# 3. 创建/切换到feature分支
git checkout -b feature/codex-$(date +%Y%m%d)

# 4. 工作...

# 5. 提交并更新HANDOFF.md
git add -A
git commit -m "[Codex] 类型: 描述"
# 更新HANDOFF.md，标记完成

# 6. 推送到GitHub
git push origin feature/codex-$(date +%Y%m%d)
# 创建PR，请求Review
```

---

## 十、附录：常见问题

**Q: 如果一方长时间不响应怎么办？**
A: 在 `HANDOFF.md` 中标记超时（如"等待Codex Review，已24小时未响应"），另一方可以单方面合并紧急修复。

**Q: 如果用户同时给双方下达矛盾指令怎么办？**
A: 以用户的最新指令为准，在 `HANDOFF.md` 中记录变更原因。

**Q: 数据库文件（.db）是否纳入Git？**
A: 否。`.db` 文件太大且不适用Git管理。Kimi负责维护数据库，Codex通过代码操作数据库。

**Q: 回测结果（CSV/PNG）是否纳入Git？**
A: 否。`reports/` 和 `signals/` 已加入 `.gitignore`。

---

*本文档由Kimi和Codex共同维护，任何修改需双方确认。*
