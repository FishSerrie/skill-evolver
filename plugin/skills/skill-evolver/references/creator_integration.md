# Skill Creator 与 Skill Evolver 联动协议

## 一、定位对比

| 维度 | Skill Creator (官方) | Skill Evolver (本 skill) | 关系 |
|---|---|---|---|
| **Create** | 采访→写 SKILL.md→生成 evals | Create 模式做同样的事 | **调用 Creator** |
| **Eval** | spawn subagent 跑 eval，人工 review viewer | 三级评测（快速门卫/开发集/严格评测），更精细 | **增强** |
| **Improve** | 人看 feedback → 手动改 → 再跑 eval | Improve 模式 = 人主导定向改进 | **调用 Creator** |
| **Benchmark** | blind A/B comparison + analyzer | Benchmark 模式 + comparator agent | **调用 Creator** |
| **Evolve** | 无。靠人循环 | **核心价值**。AutoResearch 式自动循环 | **全新** |
| **门控** | 无。人决定是否满意 | 多门控 AND 逻辑（质量/触发/成本/时延/回归） | **全新** |
| **Memory** | 只有 workspace 目录 | results.tsv + experiments.jsonl + git + best_versions | **全新** |
| **Description 优化** | `run_loop.py` 做触发优化 | Layer 1 description 优化直接调用 creator 的 run_loop | **调用 Creator** |

### 一句话总结

**Evolver = Creator 的超集。Creator 负责「单次评测循环」，Evolver 在此基础上加了「自动外循环 + 门控 + 记忆」。**

```
Creator 的循环：  人 → 改 → 跑 eval → 看 viewer → 人判断 → 改 → ...（人在回路中）
Evolver 的循环：  Search → Modify → Eval → Gate → Log → Loop → ...（人不在回路中）
```

---

## 二、调用关系（引用，非复制）

### 核心原则

**Evolver 不复制 Creator 的代码和协议。Evolver 通过引用调用 Creator 的能力。Creator 更新后，Evolver 自动生效。**

### 具体调用方式

#### 2.1 Create 模式

Evolver 的 Create 模式不自己实现创建逻辑，而是：

```
1. 读取 skill-creator 的 SKILL.md（通过 Skill tool 或直接读文件）
2. 按 creator 的 "Capture Intent → Interview → Write SKILL.md" 流程执行
3. 额外步骤：创建 evolve workspace + 生成 GT 数据模板
```

#### 2.2 Eval 模式

Evolver 的评测引擎分两部分：
- **trigger 评测**：直接调用 creator 的 `scripts/run_eval.py`
- **behavior 评测**：由 Claude 编排（spawn subagent + grader 打分）

```bash
# trigger 评测 — 直接调用 creator
python -m scripts.run_eval \
  --eval-set <workspace>/evals/trigger/trigger_eval.json \
  --skill-path <target-skill> \
  --model <model>

# behavior 评测 — Claude 编排
# 读取 creator 的 agents/grader.md 获取打分协议
# 对每个 GT case spawn subagent 执行 skill
# 用 grader 协议打分
```

#### 2.3 Description 优化（Layer 1）

直接调用 creator 的 `scripts/run_loop.py`：

```bash
python -m scripts.run_loop \
  --eval-set <workspace>/evals/trigger/trigger_eval.json \
  --skill-path <target-skill> \
  --model <model> \
  --max-iterations 5 \
  --verbose
```

#### 2.4 Benchmark / Comparison

调用 creator 的脚本和 agents：
- `scripts/aggregate_benchmark.py` — 聚合统计
- `agents/comparator.md` — 盲 A/B 比较
- `agents/analyzer.md` — 归因分析

#### 2.5 Grading

Evolver 自身的 `agents/grader_agent.md` 和 `agents/comparator_agent.md` 作为快速参考，但实际执行时读取 creator 的完整版：

```
# 在需要 grading 时，读取：
# 1. 优先读 skill-creator 的 agents/grader.md（最新完整版）
# 2. 如果 creator 不可用，fallback 到 evolver 自身的 agents/grader_agent.md
```

---

## 三、Creator 路径发现

Evolver 按以下顺序寻找 Creator 的安装位置：

```python
CREATOR_SEARCH_PATHS = [
    # 1. Marketplace plugins (official)
    "~/.claude/plugins/marketplaces/*/plugins/skill-creator/",
    # 2. Direct plugin install
    "~/.claude/plugins/skill-creator/skills/skill-creator/",
    # 3. Standalone plugin with plugin/ subdir
    "~/.claude/plugins/skill-creator/plugin/skills/skill-creator/",
    # 4. User skills directory
    "~/.claude/skills/skill-creator/",
    # 5. Project-level skills
    ".claude/skills/skill-creator/",
]
```

如果找不到 Creator：
- Create/Improve/Benchmark 模式降级为 Evolver 内置的简化版本
- Evolve 模式的核心循环不受影响（Search/Modify/Gate/Memory 是 Evolver 自有能力）
- 提示用户安装 Creator 以获得完整能力

---

## 四、哪些是 Evolver 自有能力（Creator 没有的）

| 能力 | 对应文件 | 说明 |
|---|---|---|
| **Evolve 外循环** | `references/evolve_protocol.md` | 8 阶段自动迭代 |
| **Search Agent** | `agents/search_agent.md` | 分析失败模式，生成改动假设 |
| **Analyzer Agent** | `agents/analyzer_agent.md` | 归因分析（为什么这个改动有效/无效） |
| **多门控系统** | `references/gate_rules.md` | AND 逻辑门控判定 |
| **分层 Mutation** | `references/mutation_policy.md` | Description → Body → Scripts 逐层优化 |
| **结构化 Memory** | `references/memory_schema.md` | results.tsv + experiments.jsonl |
| **自适应评测计划** | `references/eval_strategy.md` | per-skill 生成优化策略 |
| **Workspace 管理** | `scripts/setup_workspace.py` | per-skill 独立 workspace |

---

## 五、更新兼容性

### Creator 更新时，Evolver 需要做什么？

**大多数情况：什么都不用做。**

| Creator 变更类型 | Evolver 影响 | 需要操作 |
|---|---|---|
| scripts/ 内部修改 | 无（调用接口不变） | 无 |
| agents/ 协议更新 | 自动生效（引用不是复制） | 无 |
| SKILL.md 流程变更 | Create 模式自动跟随 | 无 |
| CLI 参数变更（breaking） | 调用脚本可能出错 | 更新 Evolver 的脚本调用 |
| JSON schema 变更（breaking） | 解析可能出错 | 更新 Evolver 的 schema 引用 |

**总结：只有 breaking changes 才需要 Evolver 跟进更新。**
