# Skill Evolver 技术架构方案 v2.1

> 基于 v1.1 架构方案，反映 2026-04-07 的全部重构和实现。
> v1.1 → v2.1 变更摘要见末尾「变更日志」。

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

## 三、与 Skill Creator 的关系（v2.1 新增）

### 核心原则：调用不复制

Evolver 通过**引用**调用 Creator 的能力。Creator 更新后 Evolver 自动生效。

| 维度 | Skill Creator | Skill Evolver | 关系 |
|---|---|---|---|
| Create | ✅ 采访→写 SKILL.md→生成 evals | ✅ 同 | 调用 Creator |
| Eval | ✅ spawn subagent + viewer | ✅ 三级评测 | 增强 |
| Improve | ✅ 人看 feedback → 手动改 | ✅ 同 | 调用 Creator |
| Benchmark | ✅ blind A/B | ✅ 同 | 调用 Creator |
| Evolve | ❌ 没有 | ✅ 核心 | **全新** |
| 门控 | ❌ | ✅ 多门控 AND | **全新** |
| Memory | ❌ | ✅ results.tsv + experiments.jsonl | **全新** |

### Creator 路径发现

```python
SEARCH_ORDER = [
    "~/.claude/plugins/marketplaces/*/plugins/skill-creator/",
    "~/.claude/plugins/skill-creator/plugin/skills/skill-creator/",
    "~/.claude/skills/skill-creator/",
    ".claude/skills/skill-creator/",
]
```

Creator 不可用时：Evolve 核心循环不受影响，评测能力降级为内置简化版。

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

**v2.1 变更**：
- Layer 2 从固定 L1/L2/L3 改为自适应（Quick Gate/Dev Eval/Strict Eval，由 evolve_plan.md 配置）
- Layer 3 阈值从全局默认改为 per-skill 在 evolve_plan.md 中配置

---

## 五、Workspace 机制（v2.1 重新设计）

### v1.1 设计（已废弃）

```
skill-evolver/
├── evals/       ← 放在 evolver 自身目录内
├── memory/      ← 放在 evolver 自身目录内
└── outputs/     ← 放在 evolver 自身目录内
```

### v2.1 设计（当前）

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
Phase 5: Verify   → Quick Gate + Dev Eval             [自动L1 + Claude编排L2]
Phase 6: Gate     → 多门控 keep/discard/revert         [自动: phase_6_gate_decision()]
Phase 7: Log      → 写 results.tsv + experiments.jsonl [自动: phase_7_log()]
Phase 8: Loop     → 继续/升层/结束                     [自动: phase_8_loop_control()]
```

### 自动化程度

| Phase | 自动化 | 实现 |
|---|---|---|
| 0 Setup | ✅ 全自动 | `setup_workspace.py` |
| 1 Review | ✅ 全自动 | `evolve_loop.phase_1_review()` |
| 2 Ideate | ❌ Claude 推理 | `evolve_loop.phase_2_prepare_ideation()` 准备上下文 |
| 3 Modify | ❌ Claude 执行 | — |
| 4 Commit | ✅ 全自动 | `evolve_loop.phase_4_commit()` |
| 5 L1 Gate | ✅ 全自动 | `run_l1_gate.py` |
| 5 L2 Eval | ⚠️ Claude 编排 | `run_l2_eval.py` 提供辅助函数 |
| 6 Gate | ✅ 全自动 | `evolve_loop.phase_6_gate_decision()` |
| 7 Log | ✅ 全自动 | `evolve_loop.phase_7_log()` |
| 8 Loop | ✅ 全自动 | `evolve_loop.phase_8_loop_control()` |

---

## 七、自适应评测（v2.1 替代固定 L1/L2/L3）

v1.1 写死了 L1 快速门卫/L2 开发集/L3 严格评测。v2.1 改为：

1. **evolve_plan.md 由 Claude 分析 skill 特征后生成**
2. 三类评测（Quick Gate / Dev Eval / Strict Eval）的参数全部可配
3. 不同 skill 类型有不同默认策略（见 `plugin/skills/skill-evolver/references/eval_strategy.md`）

---

## 八、分层 Mutation（不变）

同 v1.1。详见 `plugin/skills/skill-evolver/references/mutation_policy.md`。

```
Layer 1: Description → trigger F1 优化，成本低
Layer 2: SKILL.md Body → 行为质量优化，成本中
Layer 3: Scripts/References → 深层能力优化，成本高
```

---

## 九、门控规则（不变）

同 v1.1。详见 `plugin/skills/skill-evolver/references/gate_rules.md`。

核心：**AND 逻辑，所有条件必须同时满足。** 阈值由 evolve_plan.md per-skill 配置。

---

## 十、Memory 结构（路径变更）

同 v1.1 schema，但路径从 evolver 内部改到 workspace：
- `<workspace>/evolve/results.tsv`
- `<workspace>/evolve/experiments.jsonl`
- `<workspace>/evolve/best_versions/`

详见 `plugin/skills/skill-evolver/references/memory_schema.md`。

---

## 十一、中间产物清理（v2.1 新增）

### 自动清理

| 产物 | 清理规则 | 命令 |
|---|---|---|
| best_versions/ | 保留最近 3 个 | `evolve_loop.py --cleanup-versions` |
| iteration-EN/ | 保留最近 5 轮 + 所有 keep 轮 | `evolve_loop.py --cleanup` |
| git history | squash 为一个总结 commit | `cleanup_git_history()` |

### Git 防膨胀

evolve 完成后调用 `cleanup_git_history()`：
```bash
# 自动 squash 所有 experiment+revert commits
# "evolve: 65% → 78%, 6 keeps in 20 iterations"
```

---

## 十二、目录结构（v2.1 最终版）

```
skill-evolver/                          ← GitHub repo root
├── .claude-plugin/
│   ├── marketplace.json                ← 商店目录（source → ./plugin）
│   └── plugin.json                     ← 根身份证
├── plugin/                             ← Claude Code 加载的子集
│   ├── .claude-plugin/
│   │   └── plugin.json                 ← plugin 身份证
│   └── skills/
│       └── skill-evolver/
│           ├── SKILL.md (~320行)       ← 主入口 + 快速开始
│           ├── references/
│           │   ├── evolve_protocol.md  ← 8 阶段完整协议
│           │   ├── eval_strategy.md    ← 自适应评测策略模板
│           │   ├── creator_integration.md ← Creator 联动协议
│           │   ├── gate_rules.md       ← 门控规则
│           │   ├── mutation_policy.md  ← 分层 mutation 策略
│           │   └── memory_schema.md    ← Memory schema
│           ├── agents/
│           │   ├── search_agent.md     ← 搜索方向生成
│           │   ├── analyzer_agent.md   ← 归因分析
│           │   ├── grader_agent.md     ← 评分
│           │   └── comparator_agent.md ← 盲比较
│           └── scripts/
│               ├── __init__.py
│               ├── common.py           ← 共享工具
│               ├── setup_workspace.py  ← workspace 初始化
│               ├── run_l1_gate.py      ← L1 快速门卫
│               ├── run_l2_eval.py      ← L2 评测辅助函数
│               ├── aggregate_results.py ← 统计聚合
│               └── evolve_loop.py      ← 8 阶段编排 + 清理
├── docs/
│   ├── architecture-v2.1.md            ← 本文档
│   └── bootstrap-report.md             ← 自举测试报告
├── README.md
└── LICENSE
```

总计：18 个 skill 文件，~2700 行

---

## 十三、v1.1 → v2.1 变更日志

| 变更 | v1.1 | v2.1 | 原因 |
|---|---|---|---|
| evals/outputs/memory | 在 evolver 内部 | 在 per-skill workspace | 通用优化器不应携带特定 skill 数据 |
| workspace | 独立创建 | 复用 Creator 的 | 打包不带、数据共享 |
| eval 策略 | 固定 L1/L2/L3 | 自适应 evolve_plan.md | 不同 skill 需要不同策略 |
| Creator 关系 | 未定义 | 引用调用 | Creator 更新自动生效 |
| scripts/ | 空目录 | 7 个可执行脚本 | 完整自动化 |
| 快速开始 | 无 | 4 行示例 | 用户 10 秒上手 |
| 清理机制 | 无 | 3 种清理 + git squash | 防中间产物膨胀 |
| eval_levels.md | L1/L2/L3 固定定义 | eval_strategy.md 自适应 | 更灵活 |
| agents | 独立实现 | 引用 Creator + fallback | 不复制 |
| 目录结构 | 扁平 | 两层（root + plugin/） | 分离人看的和 Claude 加载的 |
| Creator 路径 | 硬编码 3 路径 | 搜索 plugins/marketplaces/skills | 适配多种安装方式 |

---

## 十四、自举测试结论

2026-04-07 执行了自举测试（用 evolver 优化 evolver 自身）：

- **5 轮手动迭代**（非自动 loop，因为 evolver 自身没有 git 管理）
- **改进落地**：快速开始、Eval 自包含、CLI 执行指引、Plan 示例精简
- **316 行**（从 344 行优化）
- **关键发现**：协议型 skill 的 GT 需要行为评测（spawn subagent），静态匹配有上限
- **所有 scripts 验证通过**：L1 gate PASS，各函数独立可调用

---

*文档版本：v2.1*
*日期：2026-04-07*
*状态：架构重构完成 + scripts 实现完成 + 自举测试通过 + GitHub repo 结构搭建完成*
*前置版本：v1.1 (2026-04-03)*
