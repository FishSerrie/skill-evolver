---
name: skill-evolver
description: "Skill 自动进化引擎 — 基于 skill-creator 评测能力 + autoresearch 自主迭代思想，自动创建、评测、迭代优化 skill。内核：Creator 做评测打分，AutoResearch 式循环做搜索优化，Evolver 加门控和记忆实现全自动进化。支持 evolve/eval/create/benchmark/improve 五种模式。Triggers on: '/skill-evolver', 'evolve skill', '进化 skill', '优化 skill', 'skill 评测', 'eval skill', 'skill benchmark', '让 skill 变强', '自动优化', 'improve skill', '改进 skill', 'create skill', '创建 skill', 'skill evolver'."
---

# Skill Evolver

一个以 GT 为中心、以 Creator 为评测范式、以 AutoResearch 为搜索制度的统一入口 Skill 自动优化器。

## 快速开始

```bash
# 全自动优化已有 skill（核心功能，一条命令跑完整 loop）
python3 scripts/evolve_loop.py ./my-skill/ --gt ./evals.json --run --max-iterations 20

# 评测已有 skill
/skill-evolver eval ./my-skill/ --gt ./evals.json

# 从零创建新 skill
/skill-evolver create

# 对比两个版本
/skill-evolver benchmark ./skill-v1/ ./skill-v2/ --gt ./evals.json
```

**前置条件：**
- GT 数据（测试用例 + assertions）推荐提前准备；若无，evolve 模式会自动调用 Creator 构造
- skill 目录**必须在 git 管理下**（若未初始化，Phase 0 强制 `git init`；若系统无 git，先安装再继续）
- skill-creator 已安装（用于评测能力）

---

## 核心理念

- **外循环搜索，内循环评测**：AutoResearch 式循环决定"改什么"，Creator 式评测度量"改得怎么样"
- **GT First**：没有 GT 数据不开始优化
- **一轮一个原子改动**：每轮只做一个可归因的变化
- **多门控，不单指标**：质量、触发、成本、时延、回归全部独立判定
- **调用 Creator，不复制**：评测/打分/比较能力来自 skill-creator，creator 更新自动生效

---

## 与 Skill Creator 的关系

**Evolver 是 Creator 的超集。** Creator 提供「单次评测循环（人在回路中）」，Evolver 在此基础上加了「自动外循环 + 门控 + 记忆（人不在回路中）」。

- Evolver 通过**引用**调用 Creator 的能力，不复制代码
- Creator 更新后 Evolver 自动生效
- 详见 `references/creator_integration.md`

**Creator 路径发现顺序：** 详见 `references/creator_integration.md` 第三节，按优先级从高到低搜索多个位置，找不到时自动降级。

---

## 五个模式

| 模式 | 触发方式 | 职责 | 调用 Creator？ |
|---|---|---|---|
| **Create** | `/skill-evolver create` | 从需求 + GT 生成初版 skill | 是：读取 Creator 的创建流程 |
| **Eval** | `/skill-evolver eval` | 单次评测，产出 benchmark | 是：调用 Creator 的 run_eval |
| **Improve** | `/skill-evolver improve` | 人主导定向改进 | 是：按 Creator 的迭代流程 |
| **Benchmark** | `/skill-evolver benchmark` | 系统对比分析（A/B、盲评） | 是：调用 Creator 的 comparator/analyzer |
| **Evolve** | `/skill-evolver evolve` | 自动循环优化（核心） | 部分：评测调用 Creator，搜索/门控/记忆是自有 |

Pipeline 是运行方式：`/skill-evolver pipeline --mode create+eval+evolve`

---

## Workspace 机制

Evolver **不在自身目录中存储任何 skill 特定数据**。它复用 Creator 已有的 workspace 目录。

### Workspace = Creator 的 workspace + Evolver 扩展

Creator 会在目标 skill 同级创建 `<skill-name>-workspace/`。Evolver 直接复用这个目录，在其中添加 evolve 相关的子目录。

```
some-project/
├── my-skill/                       ← 目标 skill（用户的，git 管理）
│   ├── SKILL.md
│   ├── references/
│   └── scripts/
└── my-skill-workspace/             ← 共享 workspace（Creator 和 Evolver 共用）
    ├── evals/                      ← Creator 已有的评测数据
    │   └── evals.json
    ├── iteration-1/                ← Creator 的评测迭代（已有）
    ├── iteration-2/
    └── evolve/                     ← Evolver 专属子目录
        ├── evolve_plan.md          ← 自适应优化计划
        ├── results.tsv             ← 实验日志
        ├── experiments.jsonl       ← 细粒度记忆
        ├── best_versions/          ← 最优 skill 快照
        ├── iteration-E1/           ← Evolve 评测产物（E 前缀区分 Creator）
        │   ├── grading.json
        │   ├── benchmark.json
        │   └── timing.json
        └── summary.md              ← 最终报告
```

**为什么共用 workspace：**
- 打包 skill 时自然不打包（workspace 是同级独立目录，不在 skill 内）
- Creator 的评测数据（evals/、iteration-N/）可被 Evolver 直接复用
- 一个 skill 的所有优化历史集中在一处

### Workspace 发现

Evolver 按以下顺序寻找 workspace：
1. `<skill-path>/../<skill-name>-workspace/`（Creator 标准位置）
2. 用户通过 `--workspace` 参数指定
3. 如果不存在，Evolver 自动创建（遵循 Creator 的命名惯例）

---

## 自适应优化计划

Evolver **不写死评测策略**。在开始优化前，会分析目标 skill 的特征，生成 `evolve_plan.md`：

### Plan 生成过程

1. 读取目标 skill 的 SKILL.md（识别 skill 类型和复杂度）
2. 读取 GT 数据（识别 assertion 类型分布、数据量、split 分布）
3. 基于以上信息生成 `evolve_plan.md`——详见 `references/eval_strategy.md` 获取模板和示例

---

## 模式详细说明

### Create 模式

调用 Creator 的创建流程 + 额外生成 GT 和 workspace。

**流程：**
1. 读取 skill-creator 的 SKILL.md，按其 "Capture Intent → Interview → Write SKILL.md" 流程执行
2. 生成初版 skill
3. **额外步骤（Evolver 独有）：**
   - 创建 evolve workspace
   - 生成初始 GT 数据模板（trigger + behavior）
   - 生成 evolve_plan.md
4. 输出：完整 skill + workspace + 建议下一步（eval or evolve）

### Eval 模式

对指定 skill 跑一次独立评测，产出质量报告。不自动进入优化循环。

**使用方式：**
```
/skill-evolver eval <skill-path> [--gt <gt-data-path>]
```

**流程：**
1. 检查 workspace 是否存在，不存在则创建
2. 如果有评测计划，读取评测策略；否则按默认策略（跑全部 dev case）
3. 按策略执行评测：
   - **Trigger 评测**：调用 skill-creator 的 `scripts/run_eval.py`（位于 Creator 安装目录下）
   - **Behavior 评测**：spawn subagent 运行 skill，用 grader 打分
4. 聚合结果，输出 benchmark
5. 调用 Creator 的 `eval-viewer/generate_review.py` 展示结果
6. 输出改进建议，但**不自动开始迭代**——用户决定下一步

### Improve 模式

人主导定向改进。调用 Creator 的迭代流程。

**流程：**
1. 读取用户的改进指令
2. 读取当前 skill + 最近 eval 结果 + experiments.jsonl
3. 按 Creator 的改进方法论做定向修改（参考 Creator SKILL.md "Improving the skill" 章节）
4. 跑一轮 Eval 验证
5. 输出对比：改前 vs 改后

### Benchmark 模式

系统对比两个版本。

**使用方式：**
```
/skill-evolver benchmark <skill-v1> <skill-v2> --gt <gt-data>
```

**流程：**
1. 对两个版本分别跑 eval
2. 调用 skill-creator 的 `scripts/aggregate_benchmark.py` 聚合（位于 Creator 安装目录下）
3. 可选：blind A/B comparison（读取本 skill 的 `agents/comparator_agent.md` 或 Creator 的完整版）
4. 可选：归因分析（读取 Evolver 的 `agents/analyzer_agent.md`）
5. 输出 benchmark 报告

### Evolve 模式（核心）

自动循环优化。Evolver 的核心价值。

**使用方式：**
```
/skill-evolver evolve <skill-path>
```
用户可能说"优化这个 skill"并给出路径，也可能直接说"这里有一些测试数据"并提供文件。GT 数据不是必须参数——没有的话 evolver 会调用 Creator 自动构造。

**核心协议详见** `references/evolve_protocol.md`。

**简要流程：**

```
Phase 0: Setup    → 创建 workspace + 生成 evolve_plan + 建立 baseline
Phase 1: Review   → 读 memory（results.tsv + experiments.jsonl + git log）
Phase 2: Ideate   → 分析失败模式，决定改什么（读 agents/search_agent.md）
Phase 3: Modify   → 做一个原子改动
Phase 4: Commit   → git commit（强制，skill 必须在 git 管理下；若无则先 git init）
Phase 5: Verify   → 按 evolve_plan 评测策略执行（调用 Creator 的评测能力）
Phase 6: Gate     → 多门控判定 keep/discard/revert（读 references/gate_rules.md）
Phase 7: Log      → 记录 results.tsv + experiments.jsonl（读 references/memory_schema.md）
Phase 8: Loop     → 继续或结束
```

**分层优化策略：**

```
Layer 1: Description（触发优化）→ 调用 Creator 的 run_loop.py
Layer 2: SKILL.md Body（行为优化）→ Evolver 自有能力
Layer 3: Scripts/References（深层能力）→ Evolver 自有能力
```

硬约束：一层改不动才进下一层。不允许跨层。详见 `references/mutation_policy.md`。

**进入 Evolve 模式后，立即开始执行 loop。不要等用户指令，不要要求用户跑命令。** 你（Claude）就是执行者：

1. 调用 `python3 scripts/setup_workspace.py <skill-path>` 创建 workspace
2. **准备 GT 数据（调用 Creator 的能力）：**
   - 检查 `<workspace>/evals/evals.json` 是否已存在 → 有就用
   - 检查用户是否在对话中提供了数据（文件路径、QA 对、样本）→ 有就基于它构造
   - 如果都没有 → **调用 skill-creator 的测试用例构造流程**：
     - 读 Creator 的 SKILL.md 中 "Test Cases" 章节的方法论
     - 按 Creator 流程：理解 skill → 写 realistic test prompts → 跑一遍 → draft assertions
     - 保存到 `<workspace>/evals/evals.json`
   - **不要自己发明构造方法** — Creator 的流程已经验证过，直接复用
   - 如果用户给了部分数据（比如几个 QA 对），先用 Creator 流程把它们转为标准 GT 格式，再补充更多 case
   - 每轮迭代中发现新边界情况时，同样用 Creator 的方法论补充 GT case
3. 读 GT 数据，对 SKILL.md 做 baseline 评测，记录 baseline 到 results.tsv
4. 开始循环：
   - 读 memory → 分析失败 → 决定改什么 → 用 Edit 做原子改动 → git commit
   - 跑 `python3 scripts/run_l1_gate.py <skill-path>` 验证
   - 逐 case 逐 assertion 打分（L2 eval）
   - 判定 keep/discard → 如果 discard 就 git revert
   - 写 results.tsv + experiments.jsonl
   - 判断是否继续
5. 循环结束后输出 summary

辅助工具（`scripts/` 里的脚本）帮你做确定性步骤，但**你自己推理决定改什么、怎么改**。

如果需要在后台无人值守运行（不在对话中），可以用 CLI 模式：
```bash
python3 scripts/evolve_loop.py <skill-path> --gt <gt-json> --run --max-iterations 20
```
这个通过 `claude -p` 子进程实现 LLM 推理。但**默认场景是你在对话中直接执行 loop**。

清理中间产物：
```bash
python3 scripts/evolve_loop.py <skill-path> --cleanup
python3 scripts/evolve_loop.py <skill-path> --cleanup-versions
```

---

## GT 数据格式

GT schema 分通用层和场景扩展层，确保 skill-evolver 适用于所有类型的 skill。

### 通用层（必须遵循）

```json
{
  "id": 1,
  "prompt": "用户的输入",
  "assertions": [
    {"type": "contains", "value": "关键内容", "description": "必须包含X"}
  ],
  "split": "dev",
  "metadata": {}
}
```

### 通用断言类型

| type | 说明 |
|---|---|
| `contains` | 输出包含指定文本 |
| `not_contains` | 输出不能包含指定文本 |
| `regex` | 输出匹配正则 |
| `path_hit` | 引用了正确的文档路径 |
| `fact_coverage` | 覆盖了指定关键事实点 |
| `script_check` | 运行脚本检查输出 |
| `json_schema` | 输出符合 JSON schema |
| `file_exists` | 生成了指定文件 |

### split 字段

必须标注 `"dev"` / `"holdout"` / `"regression"`。分组策略在 evolve_plan.md 中定义。

---

## 门控规则

详见 `references/gate_rules.md`。

核心原则：**所有 Keep 条件必须同时满足（AND 逻辑）**。门控阈值在 evolve_plan.md 中 per-skill 定义。

---

## Memory 结构

详见 `references/memory_schema.md`。

Memory 存储在目标 skill 的 workspace `evolve/` 子目录中，不在 evolver 自身目录中：
- `<workspace>/evolve/results.tsv`：实验日志
- `<workspace>/evolve/experiments.jsonl`：细粒度记忆
- `<workspace>/evolve/best_versions/`：历史最优快照

---

## Reference 文件索引

| 文件 | 内容 | 何时读取 |
|---|---|---|
| `references/evolve_protocol.md` | Evolve 8 阶段完整协议 | 进入 Evolve 模式时 |
| `references/eval_strategy.md` | 自适应评测策略模板 | 生成 evolve_plan 时 |
| `references/gate_rules.md` | 多门控规则 + 伪代码 | Gate 判定时 |
| `references/mutation_policy.md` | 分层 mutation 策略 | 决定改什么时 |
| `references/memory_schema.md` | results.tsv + experiments.jsonl schema | 读写 memory 时 |
| `references/creator_integration.md` | 与 Creator 的联动协议 | 需要调用 Creator 能力时 |
| `agents/search_agent.md` | 变体生成协议 | Phase 2 Ideate 时 |
| `agents/grader_agent.md` | 评分协议（快速参考，完整版见 Creator） | 评测打分时 |
| `agents/comparator_agent.md` | 盲 A/B 比较（快速参考，完整版见 Creator） | Benchmark 模式时 |
| `agents/analyzer_agent.md` | 归因分析协议 | 分析改动效果时 |
