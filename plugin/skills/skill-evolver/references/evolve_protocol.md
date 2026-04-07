# Evolve 核心协议（8 阶段）

本文档定义 Evolve 模式的完整执行协议。每一轮迭代严格按 Phase 0-8 执行。

---

## Phase 0: 前置检查

在开始任何迭代前，必须确认：

1. **skill 目录完整**：SKILL.md 存在，目录结构合法
2. **GT 数据就绪**：存在 assertions、有 dev/holdout split
3. **git 状态干净**：`git status` 无未提交变更
4. **workspace 就绪**：复用 Creator 的 `<skill-name>-workspace/`，确认 `evolve/` 子目录存在
   - 可调用 `python3 scripts/setup_workspace.py <skill-path>`
5. **生成 evolve_plan.md**：分析 skill 和 GT 数据，生成自适应优化计划（详见 `references/eval_strategy.md`）
6. **确定当前 mutation layer**：
   - 由 `evolve_plan.md` 的优化优先级决定起始 layer
   - 上一层连续 K 轮（K 由 plan 指定，默认 5）无提升 → 升级
7. **建立 baseline**（仅首次）：
   - 按 evolve_plan.md 策略跑一轮评测
   - 记录 baseline 到 `<workspace>/evolve/results.tsv`（iteration 0）
   - 保存当前 skill 到 `<workspace>/evolve/best_versions/`

---

## Phase 1: Review（读 Memory，30秒内完成）

每轮迭代开始时，必须读取：

```bash
# 1. 最近的实验历史
git log --oneline -20

# 2. 结果日志
tail -20 <workspace>/evolve/results.tsv

# 3. 细粒度记忆（如果存在）
tail -10 <workspace>/evolve/experiments.jsonl
```

**从 memory 中识别：**
- 哪些改动类型成功了（status=keep）→ 可以 exploit
- 哪些改动类型失败了（status=discard）→ 避免重复
- 哪些 case 始终失败 → 重点关注
- 哪些 case 容易被改坏 → 作为 regression 守护
- 是否 stuck（连续 5+ 轮 discard）→ 需要 radical 策略

---

## Phase 2: Ideate（决定改什么）

基于 Phase 1 的分析，按优先级选择改动方向：

**优先级排序：**

1. **修复 crash**：上轮有 crash 的 case → 优先修复
2. **exploit 成功模式**：上轮 keep 的改动类型 → 尝试同类变体
3. **攻克顽固失败 case**：多轮始终失败的 case → 针对性改进
4. **explore 新方向**：交叉参考 results + git → 找未尝试的方向
5. **simplify**：删减 skill 中不起作用的部分，保持指标不降
6. **radical**：当 stuck 时（连续 5+ discard），做大胆尝试

**输出：**
- 一句话描述改动意图
- mutation_type（如 body_rewrite / body_simplify / rule_reorder / template_change）
- 改动范围（哪些文件）

**Anti-patterns（禁止）：**
- 不允许重复已被 discard 的完全相同的改动（先查 git log）
- 不允许一轮做多个不相关改动（一句话测试：需要用"and"描述 → 说明是两个改动）
- 不允许跨 mutation layer 改动
- **不允许"发现了问题但不修"** — 只要识别出是问题，就应该作为一次迭代去修复，不区分大小。迭代的意义就是持续改进，跳过"小问题"等于放弃了改进机会

---

## Phase 3: Modify（一个原子改动）

执行 Phase 2 确定的改动。

**规则：**
- 只改当前 layer 的文件
- 改动必须可用一句话解释
- 改完后自检：
  - `git diff --stat` 检查影响范围
  - 超过 5 个文件 → 大概率不是原子改动，需要拆分

**改动技巧：**
- 优先解释 why 而不是硬写 MUST/NEVER
- 优先改结构/流程，而不是堆更多文字
- 如果发现多个 case 都独立写了相同的 helper 逻辑 → 提取成 script

---

## Phase 4: Commit

```bash
git add <changed-files>
git commit -m "experiment(<layer>): <一句话描述>"
```

例如：
```
experiment(body): 增加跨分类检索时的路径合并规则
experiment(body): 简化 Stage 2 的节点选择提示
experiment(description): 增加对打卡异常场景的触发覆盖
```

**git 优先策略（三级决策树）：**

按顺序检查，能用 git 就用 git，最后才降级：

**Step 1：检查目录是否在 git 管理下**
```bash
git -C <skill-path> rev-parse --is-inside-work-tree 2>/dev/null
```
- ✅ 已在 git 管理下 → 直接进入 Phase 1，无需任何操作

**Step 2：有 git 但未 init → 立即 init**
```bash
git --version 2>/dev/null  # 检查 git 是否安装
```
- ✅ git 已安装，只是没有 init → **直接 git init，不跳过，不降级**：
```bash
cd <skill-path>
git init
git add .
git commit -m "chore: init git for evolve tracking"
```

**Step 3：git 未安装 → 尝试安装**
- 提示用户安装，同时继续等待，不自动降级：
```
⚠️ 未检测到 git。请安装后重试：
  macOS:  brew install git  或  xcode-select --install
  Ubuntu: sudo apt-get install git
  CentOS: sudo yum install git
  Windows: https://git-scm.com/download/win
```

**Step 4：git 无法安装（无网络/受限环境） → 降级**
- 仅在确认 git 无法安装时启用文件夹备份：
  1. 备份修改前的文件到 `<workspace>/evolve/best_versions/pre-iteration-N/`
  2. 在 experiments.jsonl 中记录关键行变更
  3. Gate 判定为 discard 时手动用备份恢复
- **在 results.tsv 中标注 `[no-git]`，提醒用户后续补装 git 重跑**



## Phase 5: Verify（按 evolve_plan.md 评测策略执行）

评测策略不写死，由 `<workspace>/evolve/evolve_plan.md` 定义。以下是三类可配置评测：

### Quick Gate（每轮必跑，秒级）

可调用 `python3 scripts/run_l1_gate.py <skill-path> [--gt <gt-json>]`：
- skill 文件语法正确（YAML frontmatter 合法）
- 不包含明显的破坏性变更
- trigger 快速抽样（样本数由 evolve_plan 指定）
- hard assertions 快速检查（核心 GT case 抽样）

**Quick Gate 失败 → 直接跳到 Phase 6 discard，不跑 Dev Eval。**

### Dev Eval（频率由 evolve_plan 定义，分钟级）

由 Claude 编排（spawn subagent + grader 打分），`scripts/run_l2_eval.py` 提供辅助函数：

1. **执行**：spawn subagent，加载 skill，运行每个 prompt
2. **打分**：读取 `agents/grader_agent.md`（或 Creator 的完整版），按 assertions 逐条判定
3. **采集 timing**：记录 tokens 和 duration
4. **聚合**：`run_l2_eval.aggregate_grades()` → benchmark.json
5. **重点关注**：evolve_plan.md 中标记的高优先级 assertion 类型

### Strict Eval（触发条件由 evolve_plan 定义，十分钟级）

触发条件（由 evolve_plan.md 配置）：
- 每 N 轮自动触发
- 或 Dev Eval pass_rate 超过阈值时
- 或准备做 layer promotion 前

内容：
- 跑 holdout set（split="holdout"）
- 跑 regression set（split="regression"）
- 可选：blind A/B comparison（读取 `agents/comparator_agent.md`）

---

## Phase 6: Gate（多门控判定）

读取 `references/gate_rules.md` 获取完整门控逻辑。

**简化版判定：**

```
IF crash or timeout → REVERT
IF L1 fail → DISCARD
IF L2 pass_rate > baseline.pass_rate + min_delta
   AND trigger not degraded
   AND tokens <= baseline × 1.2
   AND duration <= baseline × 1.2
   AND regression not broken
   → KEEP
ELSE → DISCARD
```

**Keep 动作：**
- 更新 baseline 为当前版本
- 保存 skill 快照到 best_versions/

**Discard 动作：**
```bash
git revert HEAD --no-edit
```
注意：用 `git revert` 而不是 `git reset`，保留失败实验的历史。

**Revert 动作（crash/严重退化）：**
```bash
git revert HEAD --no-edit
```
并在 experiments.jsonl 中记录 crash 原因。

---

## Phase 7: Log

### results.tsv

```bash
echo -e "${iteration}\t${commit}\t${metric}\t${delta}\t${trigger_f1}\t${tokens}\t${guard}\t${status}\t${layer}\t${description}" >> <workspace>/evolve/results.tsv
```

### experiments.jsonl

```bash
echo '{"iteration":N,"mutation_type":"...","mutation_layer":"...","intent":"...","cases_improved":[...],"cases_degraded":[...],"trigger_delta":0.0,"token_delta":0,"status":"keep/discard"}' >> <workspace>/evolve/experiments.jsonl
```

### Progress Summary（每 10 轮）

```
=== Skill Evolve Progress (iteration 20) ===
Baseline: 65.0% → Current best: 78.0% (+13.0%)
Keeps: 6 | Discards: 12 | Crashes: 2
Current layer: body
Last 5: keep, discard, discard, keep, keep
```

---

## Phase 8: Loop

- **bounded**：到达 max_iterations → 输出 summary + best skill
- **unbounded**：继续 Phase 1
- **layer promotion**：当前 layer 连续 K 轮（默认 5）无 keep → 升级到下一 layer
- **stuck detection**：连续 5 轮 discard → 切换到 radical 策略（Priority 6）
- **exhaustion**：3 个 layer 都尝试过且无提升 → 输出最终报告并结束

---

## 终止时输出

Evolve 结束时，必须输出：

1. **best_skill/**：当前最优版本的完整 skill 目录
2. **results.tsv**：完整实验日志
3. **experiments.jsonl**：细粒度记忆
4. **summary.md**：
   - baseline → best 的提升幅度
   - 有效改动列表
   - 无效改动列表
   - 各 layer 的 keep/discard 比例
   - 建议下一步优化方向

---

## 中间产物清理

Evolve 过程中会产生大量中间产物（git commits、best_versions 快照、evaluation 产物）。终止后应清理：

### 自动清理规则

1. **best_versions/**：只保留最近 3 个快照，更早的自动删除
2. **iteration-EN/ 评测产物**：只保留最近 5 轮和所有 keep 轮的产物，其余删除
3. **git history**：**不自动清理**（git revert 已保留完整历史，可手动 squash）

### 手动清理命令

```bash
# 清理评测产物（保留最近 5 轮 + 所有 keep 轮）
python3 scripts/evolve_loop.py <skill-path> --cleanup

# 清理 best_versions（只保留最新 3 个）
python3 scripts/evolve_loop.py <skill-path> --cleanup-versions

# 完全清理（删除整个 evolve/ 子目录，保留 Creator 的数据）
rm -rf <workspace>/evolve/
```

### Git 清理建议

Evolve 完成后，如果要清理 git 历史中的 experiment 和 revert commits：
```bash
# 找到 evolve 开始前的 commit
git log --oneline | grep -v "experiment\|Revert" | head -1

# 交互式 rebase 到那个点（可选，非必须）
# git rebase -i <commit-before-evolve>
```

**注意：不建议在 evolve 进行中清理 git。中间产物是 memory 的一部分。**
