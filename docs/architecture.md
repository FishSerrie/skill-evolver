[English](../README.md) · [中文](README_CN.md) · **技术架构** · [Architecture (EN)](architecture.en.md)

---

# Skill Evolver 技术架构

> 发布版本：v1.0
> 最后更新：2026-04-08

---

## 一、项目目标

构建一个统一入口的 Skill Evolver：

- 从任务描述 + GT 数据创建初版 skill
- 基于分层评测自动评估 skill 表现
- 基于 AutoResearch 式外循环自动迭代 skill
- 在多门控约束下持续产出当前最优 skill 版本

**对外**：一个 skill，一个入口
**对内**：5 个模式 + pipeline 编排 + 四层架构

---

## 二、产品形态

### 2.1 五个模式

| 模式 | 职责 | 适用场景 |
|---|---|---|
| **Create** | 从需求+GT生成初版 skill | 新建 skill |
| **Eval** | 单次独立评测，产出 benchmark | 了解当前质量 |
| **Improve** | 人主导定向改进 | 半自动优化 |
| **Benchmark** | 系统对比分析（A/B、盲评） | 版本对比决策 |
| **Evolve** | 自动循环优化（核心价值） | 无人值守持续改进 |

### 2.2 快速开始

```bash
/skill-evolver eval ./my-skill/ --gt ./evals.json
/skill-evolver evolve ./my-skill/ --gt ./evals.json --iterations 20
/skill-evolver create
/skill-evolver benchmark ./skill-v1/ ./skill-v2/ --gt ./evals.json
```

### 2.3 Pipeline

```
/skill-evolver pipeline ./my-skill/ --mode create+eval+evolve
```

---

## 三、与 Skill Creator 的关系（硬依赖）

### 核心原则：引用调用，绝不复制 — Creator 是硬依赖

Evolver 通过**引用**调用 Creator 的能力。Creator 更新后 Evolver 自动生效——零复制、零漂移。**没有 fallback 路径。** Creator 找不到时 `require_creator()` 直接抛 `CreatorNotFoundError` 并显示安装指引。

| 维度 | Skill Creator | Skill Evolver | 关系 |
|---|---|---|---|
| Create | ✅ 采访→写 SKILL.md→生成 evals | ✅ 同 | 调用 Creator |
| Eval | ✅ spawn subagent + viewer | ✅ 三级评测 | 增强 |
| Improve | ✅ 人看 feedback → 手动改 | ✅ 同 | 调用 Creator |
| Benchmark | ✅ blind A/B | ✅ 同 | 调用 Creator |
| Evolve | ❌ 没有 | ✅ 核心 | **全新** |
| 门控 | ❌ | ✅ 多门控 AND | **全新** |
| Memory | ❌ | ✅ results.tsv + experiments.jsonl + traces | **全新** |

### Creator 路径发现（`scripts/common.py:find_creator_path`）

```python
# 优先级 0：用户通过环境变量指定
os.environ.get("SKILL_CREATOR_PATH")

CREATOR_SEARCH_PATHS = [
    "~/.claude/plugins/marketplaces/*/plugins/skill-creator/skills/skill-creator/",
    "~/.claude/skills/skill-creator/",
    ".claude/skills/skill-creator/",
    "/tmp/anthropic-skills-latest/skills/skill-creator/",
]
```

**没有 fallback。没有静默降级。** Creator 找不到时：

```python
from common import require_creator, CreatorNotFoundError
try:
    creator = require_creator()  # 首次解析后会缓存
except CreatorNotFoundError as e:
    # 错误信息包含：
    # - GitHub URL: https://github.com/anthropics/skills/tree/main/skills/skill-creator
    # - 三种安装方式（插件市场 / git clone / 环境变量）
    # - 完整的搜索路径列表
    print(e); sys.exit(2)
```

非标准路径用户可通过：
- 环境变量：`export SKILL_CREATOR_PATH=/custom/path`
- CLI 参数：`evolve_loop.py --creator-path /custom/path`

**为什么硬依赖而不是 fallback**：把 Creator 的 grader/comparator 复制到 Evolver 内会导致——Creator 更新协议时副本立即过时漂移。改为运行时硬依赖后，每次跑都用 Creator 最新版协议。Evolver 的 `agents/grader_agent.md` 和 `agents/comparator_agent.md` 现在是纯指针文件，运行时通过 `get_creator_agent_path()` 读 Creator 完整版。

---

## 四、核心架构（四层）

```
┌──────────────────────────────────────────────────────┐
│  Layer 4: Search（AutoResearch 外循环）                │
│  职责：决定改什么、怎么改、改完是否保留                  │
│  ├── 读 memory（results.tsv + experiments.jsonl）     │
│  ├── 分析失败模式（哪类 case 变差？）                   │
│  ├── 生成一个原子改动                                  │
│  ├── 应用改动 → git commit                            │
│  └── 调用 Layer 3 评测 → 回到门控决策                  │
├──────────────────────────────────────────────────────┤
│  Layer 3: Gate（多门控决策层）                          │
│  职责：keep / discard / revert 的硬判断                │
│  ├── 质量是否提升（pass_rate）                         │
│  ├── trigger 是否退化（F1）                            │
│  ├── token/latency 是否超阈值                          │
│  ├── regression 是否破坏已有能力                        │
│  └── 阈值由 evolve_plan.md per-skill 配置              │
├──────────────────────────────────────────────────────┤
│  Layer 2: Eval（自适应评测引擎）                        │
│  职责：把 skill 测清楚，产出可比较的度量                 │
│  ├── Quick Gate（YAML + trigger 抽样，秒级）            │
│  ├── Dev Eval（behavior GT，分钟级）                    │
│  ├── Strict Eval（holdout + regression，十分钟级）      │
│  └── 策略由 evolve_plan.md 定义，不写死                 │
├──────────────────────────────────────────────────────┤
│  Layer 1: Memory（结构化实验记忆）                      │
│  职责：避免重复失败、利用成功模式                        │
│  ├── results.tsv（每轮一行实验日志）                    │
│  ├── experiments.jsonl（per-case 细粒度记录）           │
│  ├── git history（版本快照 + commit message）          │
│  └── best_versions/（历史最优 skill 快照）             │
└──────────────────────────────────────────────────────┘
```

Layer 2 使用**自适应三级评测**（Quick Gate / Dev Eval / Strict Eval），不走固定 L1/L2/L3，由 `evolve_plan.md` per-skill 配置。Layer 3 阈值同样 per-skill 在 `evolve_plan.md` 中定义。

---

## 五、Workspace 机制

**Evolver 自身目录不存储任何 skill 特定数据。** 数据放在目标 skill 的 workspace 中。

```
some-project/
├── my-skill/                       ← 目标 skill（git 管理）
└── my-skill-workspace/             ← 与 Creator 共享的 workspace
    ├── evals/evals.json            ← Creator 格式 GT 数据
    ├── iteration-1/                ← Creator 的评测迭代
    └── evolve/                     ← Evolver 专属子目录
        ├── evolve_plan.md          ← 自适应优化计划
        ├── results.tsv             ← 实验日志
        ├── experiments.jsonl       ← 细粒度记忆
        ├── best_versions/          ← 最优 skill 快照
        ├── iteration-E1/           ← Evolve 评测产物（E 前缀区分）
        └── summary.md              ← 最终报告
```

**设计决策**：
- 复用 Creator 的 `<skill-name>-workspace/`，不另建目录
- Evolver 数据集中在 `evolve/` 子目录，和 Creator 的 `iteration-N/` 不冲突
- 打包 skill 时 workspace 自然不包含在内

---

## 六、Evolve 模式核心协议（8 阶段）

完整协议见 `plugin/skills/skill-evolver/references/evolve_protocol.md`。

### 流程概览

```
Phase 0: Setup    → 创建 workspace + 生成 evolve_plan + 建立 baseline
Phase 1: Review   → 读 memory                        [自动: phase_1_review()]
Phase 2: Ideate   → 分析失败，决定改什么               [Claude 推理]
Phase 3: Modify   → 做一个原子改动                     [Claude 执行]
Phase 4: Commit   → git commit                        [自动: phase_4_commit()]
Phase 5: Verify   → 三层测评（Quick Gate → Dev Eval → 条件触发 Strict Eval）
Phase 6: Gate     → 多门控 keep/discard/revert         [自动: phase_6_gate_decision()]
Phase 7: Log      → 写 results.tsv + experiments.jsonl [自动: phase_7_log()]
Phase 8: Loop     → 继续/升层/结束                     [自动: phase_8_loop_control()]
```

### Phase 5 三层测评（L1/L2/L3 ≡ Quick Gate/Dev Eval/Strict Eval）

> L1/L2/L3 是脚本文件名的历史命名（`run_l1_gate.py`、`run_l2_eval.py`）；Quick Gate / Dev Eval / Strict Eval 是文档里的概念名。两套叫法指向**完全相同**的东西。

| 标签 | 别名 | 作用 | 速度 | 频率 | 实现 |
|---|---|---|---|---|---|
| **L1** | Quick Gate | 语法/结构/Creator `quick_validate` 秒级门卫 | 秒级 | 每轮必跑 | `run_l1_gate.py` |
| **L2** | Dev Eval | dev split 逐条断言打分（程序 + BinaryLLMJudge） | 分钟级 | 每轮 / 按 plan 频率 | `run_l2_eval.py` + `evaluators.py` |
| **L3** | Strict Eval | holdout + regression + 可选盲评 A/B | ~10 分钟 | 条件触发（按 plan） | 复用 `run_l2_eval.py` + 换 split |

### 自动化程度

| Phase | 自动化 | 实现 |
|---|---|---|
| 0 Setup | ✅ 全自动 | `setup_workspace.py` |
| 1 Review | ✅ 全自动 | `evolve_loop.phase_1_review()` |
| 2 Ideate | ❌ Claude 推理 | `evolve_loop.phase_2_prepare_ideation()` 准备上下文 |
| 3 Modify | ❌ Claude 执行 | — |
| 4 Commit | ✅ 全自动 | `evolve_loop.phase_4_commit()` |
| 5 L1 / Quick Gate | ✅ 全自动 | `run_l1_gate.py` |
| 5 L2 / Dev Eval | ⚠️ Claude 编排 | `run_l2_eval.py` + `evaluators.py` |
| 5 L3 / Strict Eval | ⚠️ Claude 编排（条件触发） | 复用 L2 脚本 + 不同 split |
| 6 Gate | ✅ 全自动 | `evolve_loop.phase_6_gate_decision()` |
| 7 Log | ✅ 全自动 | `evolve_loop.phase_7_log()` |
| 8 Loop | ✅ 全自动 | `evolve_loop.phase_8_loop_control()` |

---

## 七、自适应评测

评测策略不写死，全部通过 `evolve_plan.md` 参数化：

1. **evolve_plan.md 由 Claude 分析 skill 特征后生成**
2. 三类评测（Quick Gate / Dev Eval / Strict Eval）的参数全部可配——样本数、超时预算、断言权重、通过阈值
3. 不同 skill 类型有不同默认策略（见 `plugin/skills/skill-evolver/references/eval_strategy.md`）

---

## 八、分层 Mutation

详见 `plugin/skills/skill-evolver/references/mutation_policy.md`。

每次**原子改动**只针对一个 mutation layer：

```
Layer 1: Description → trigger F1 优化，成本低
Layer 2: SKILL.md Body → 行为质量优化，成本中
Layer 3: Scripts/References → 深层能力优化，成本高
```

外循环只在下层 plateau 时才升级到上层。

---

## 九、门控规则

详见 `plugin/skills/skill-evolver/references/gate_rules.md`。

核心：**AND 逻辑，所有条件必须同时满足。** 质量提升但造成 regression 的改动会被 discard。阈值由 `evolve_plan.md` per-skill 配置。

---

## 十、Memory 结构

所有 memory 产物都放在目标 skill 的 workspace 里（不在 evolver 自身目录）：
- `<workspace>/evolve/results.tsv` — 每轮一行
- `<workspace>/evolve/experiments.jsonl` — per-case 细粒度记录 + 诊断
- `<workspace>/evolve/best_versions/` — 历史最优版本快照
- `<workspace>/evolve/iteration-E{N}/traces/` — per-case 执行 trace（Meta-Trace）

详见 `plugin/skills/skill-evolver/references/memory_schema.md`。

---

## 十一、中间产物清理

### 自动清理

| 产物 | 清理规则 | 命令 |
|---|---|---|
| best_versions/ | 保留最近 3 个 | `evolve_loop.py --cleanup-versions` |
| iteration-EN/ | 保留最近 5 轮 + 所有 keep 轮 | `evolve_loop.py --cleanup` |

Git 历史不自动清理——`git revert` 已经完整保留了失败实验的 history，手动 squash 是可选优化（参考 `references/evolve_protocol.md` 的 Git Cleanup Recommendations 段）。

---

## 十二、目录结构

```
skill-evolver/                          ← GitHub repo root
├── .claude-plugin/
│   └── marketplace.json                ← marketplace 目录（source → ./plugin）
├── plugin/                             ← Claude Code 加载的子集
│   ├── .claude-plugin/
│   │   └── plugin.json                 ← plugin 身份证（per Claude Code plugin format spec）
│   └── skills/
│       └── skill-evolver/
│           ├── SKILL.md                ← 主入口 + 快速开始
│           ├── references/
│           │   ├── evolve_protocol.md       ← 8 阶段完整协议
│           │   ├── eval_strategy.md         ← 自适应评测策略模板
│           │   ├── creator_integration.md   ← Creator 联动协议（硬依赖）
│           │   ├── gate_rules.md            ← 门控规则
│           │   ├── mutation_policy.md       ← 分层 mutation 策略
│           │   └── memory_schema.md         ← Memory schema
│           ├── agents/
│           │   ├── search_agent.md          ← 搜索方向生成 + active diagnosis
│           │   ├── analyzer_agent.md        ← 归因分析
│           │   ├── grader_agent.md          ← 指针文件 → Creator 的 grader.md
│           │   └── comparator_agent.md      ← 指针文件 → Creator 的 comparator.md
│           └── scripts/
│               ├── __init__.py
│               ├── common.py                ← 共享工具 + require_creator()
│               ├── setup_workspace.py       ← workspace 初始化
│               ├── run_l1_gate.py           ← L1 快速门卫（调 Creator 的 quick_validate）
│               ├── run_l2_eval.py           ← L2 评测辅助函数
│               ├── evaluators.py            ← LocalEvaluator 框架 + BinaryLLMJudge
│               ├── aggregate_results.py     ← 统计聚合
│               └── evolve_loop.py           ← 8 阶段编排 + eval viewer 启动
├── .agents/skills/skill-evolver/       ← Codex 平台变体（自动同步）
├── .opencode/skills/skill-evolver/     ← OpenCode 平台变体（自动同步）
├── docs/
│   ├── architecture.md                 ← 本文档（中文）
│   └── architecture.en.md              ← 本文档（英文）
├── README.md
└── LICENSE
```

总计：18 个 skill 文件，~2700 行

---

*发布版本：v1.0*
*日期：2026-04-08*
