# Skill Evolver — Claude Code 项目规则

## Git 工作流 SOP（强制执行）

### 核心纪律

1. **禁止直接 push**。所有 commit 只提交到本地，由用户自行 push。
2. **main 只接收整理后的 commit**，不接收开发碎 commit。

### Commit 分层

| 层级 | 含义 | 允许存在位置 |
|------|------|-------------|
| **临时 commit** | 开发中的检查点，不保证美观 | 功能分支 only |
| **候选 commit** | 准备合并前整理后的 commit | 功能分支（squash 后） |
| **主线 commit** | 只允许候选 commit 进入 | main |

### 标准工作流程（6 步）

#### Step 1: 开分支
新任务先开分支，不直接在 main 上工作。
```bash
git checkout -b feat/xxx   # 功能
git checkout -b fix/xxx    # 修复
git checkout -b exp/xxx    # 实验
```

#### Step 2: 开发中自由提交
在分支上允许频繁小 commit，目标是可回滚、可验证。
提交信息可以偏短，重点是保留检查点。

#### Step 3: 提交前自测
至少跑和改动直接相关的测试/验证。

#### Step 4: 准备合并时整理历史
把分支上的碎 commit 压成 **1-3 个语义完整的 commit**：
```bash
# 方式 A：soft reset（推荐，简单直接）
git reset --soft main
git commit -m "feat: ..."

# 方式 B：rebase -i（需要保留 2-3 个有意义的节点时）
git rebase -i main
# 在编辑器中把不需要的 commit 标记为 squash/fixup
```

#### Step 5: 整理后再次验证
squash 后必须再跑一次关键测试，确认没有丢东西。

#### Step 6: 合并到 main
```bash
git checkout main
git merge --ff-only feat/xxx   # 优先 fast-forward
git branch -d feat/xxx         # 删除已合并分支
```
然后告知用户："已合并到本地 main，你可以 `git push` 了。"

### Commit Message 规范

```
<type>: <简要描述>（不超过 72 字符）

- 变更点 1
- 变更点 2

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
```

- **type** 可选：feat / fix / refactor / docs / chore / test
- 英文，首字母小写
- body 用 bullet points 列出主要变更

### 版本号

- 主版本按 v0.x 递增（v0.6 → v0.7 → ...）
- 小版本用 v0.x.y（如 v0.6.1）表示 hotfix
- 版本号变更时同步更新 docs 中的版本标注

### 特殊情况

- **如需改写远端分支**：只用 `git push --force-with-lease`，不要裸 `--force`
- **main 历史重写**：仅仓库 owner 执行，执行前先打 backup tag
- **分支历史可自由重写**，主线历史谨慎重写

### 安全网

- 重写历史前**必须**先创建 backup tag：`git tag backup-before-xxx HEAD`
- 确认无误后再删除 backup tag：`git tag -d backup-before-xxx`
