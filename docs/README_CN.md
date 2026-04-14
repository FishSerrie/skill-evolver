[English](../README.md) · **中文** · [技术架构](architecture.md)

# Skill Evolver

> **Point it at a skill. Wake up to a better skill.**
>
> 给它一个 skill，告诉它 GT 数据在哪里，它会在 8 阶段循环里自动搜索 → 修改 → 评测 → 门控 → 保留或丢弃，直到收敛或达到最大迭代数。**全程零人工干预。**

```
            ┌──────────────────┐
            │    Your Skill    │
            └────────┬─────────┘
                     │
                     ▼
    ┌──────────────────────────────────┐
    │          Skill Evolver           │
    │                                  │
    │    search → modify → evaluate    │
    │      → gate → keep/discard       │
    │             → repeat             │
    └────────────────┬─────────────────┘
                     │
                     ▼
            ┌──────────────────┐
            │  A Better Skill  │
            └──────────────────┘
```

---

## 一句话理解：用训模型的思路训你的 Skill

如果你做过机器学习，你已经懂了 Skill Evolver 在干什么。

| 训模型 | 训 Skill（Skill Evolver） |
|---|---|
| Training data（训练数据） | **GT（Ground Truth）** — evals.json 里的 test cases + assertions |
| 定义 loss function | **8 种 assertion 类型 + 5 维 AND 门控** — 不是单一数字，是多维度的"这个 skill 到底好不好"的定义 |
| Train（梯度下降 / 迭代） | **8 阶段 loop** — search → modify → evaluate → gate → keep/discard → repeat |
| 选 checkpoint | **best_versions/** — 每次 keep 的版本快照，最后选最好的 |
| Overfitting 检测 | **holdout split**（Anti-Goodhart）— 迭代期间绝不暴露给 proposer |
| Regression test | **regression split** — 防止改好了 A 坏了 B |
| Learning rate / 搜索步长 | **分层 mutation**（description → body → scripts）— 从小改到大改 |
| Early stopping | **stuck detection + convergence** — 连续 N 轮不 keep 就升层或停止 |

**关键的一点**：这不是让 skill 的"语法检查"通过率更高。就像训模型不是让代码跑通 —— 是**让 skill 更贴合你的数据**。你的 GT 定义了"好的 skill 应该在什么输入下产出什么结果"，Skill Evolver 的 loop 让 skill 不断逼近那个目标，就像 SGD 让模型逼近 loss 的最小值。

**你拿到的不只是一个"能跑"的 skill，而是一个在你定义的评测维度上被充分训练过的 skill。**

---

## 为什么需要 Skill Evolver？

为了解决全手工 skill 优化问题——每次依赖人工 review，发现不对再改，再测，如此反复。skill 质量严重依赖作者水平，**不可复现，不可扩展，不可审计**。

### 业界相关支撑思路

| 支柱 | 来源 | 提供给我们的思路 / 能力 |
|---|---|---|
| **评测引擎** | [skill-creator](https://github.com/anthropics/skills/tree/main/skills/skill-creator)（Anthropic 官方） | 评测、打分、对比协议；测试用例设计、完整评测体系 |
| **自主迭代外循环** | [Karpathy autoresearch](https://github.com/karpathy/autoresearch) → [uditgoenka/autoresearch](https://github.com/uditgoenka/autoresearch)（skill 化） | `modify → verify → keep/discard → repeat` 8 阶段外循环，5 条原则：one metric / constrained scope / fast verification / automatic rollback / git as memory |
| **失败诊断思路**（受 Meta-Harness 启发） | [Stanford Meta-Harness](https://arxiv.org/pdf/2603.28052)（Lee et al. 2026） | **论文原话**："access to raw execution traces is the key ingredient for enabling harness search"。论文 Table 3 ablation：只给分数 34.6 → 加摘要 34.9（几乎没动）→ 给完整 trace **50.0**。**我们借鉴的思路**：不要只给 proposer 分数，要把评测过程的原始信息（每个 case 的 prompt + skill 输出 + per-assertion PASS/FAIL）结构化暴露给它，让诊断建立在"看现场"而不是"猜分数"上 |

### Skill Evolver 的增量贡献

1. **5 维 AND 门控**：质量 / 触发 F1 / 成本 / 时延 / 回归全部通过才 keep，任一不过即 `git revert`
2. **Workspace git 隔离**：实验 commit 落独立 git，零项目污染
3. **Meta-evolution 自证**：用自己迭代自己 22 轮（v1 88.9%→100%，v2 71/71 全绿 0 crash），每轮都发现作者看不见的 bug

---

## 快速开始

### 1. 自动进化已有 skill（核心功能）

```bash
# 一条命令，完整循环——运行到收敛或达到最大迭代次数
/skill-evolver evolve ./my-skill/
```

或通过 CLI 无人值守执行：

```bash
python3 scripts/evolve_loop.py ./my-skill/ --gt ./evals.json --run --max-iterations 20
```

### 2. 评测一个 skill

```bash
/skill-evolver eval ./my-skill/ --gt ./evals.json
```

### 3. 从零创建新 skill

```bash
/skill-evolver create
```

### 4. 对比两个版本

```bash
/skill-evolver benchmark ./skill-v1/ ./skill-v2/ --gt ./evals.json
```

---

## 工作原理

### 进化循环（8 个阶段）

```
Phase 0: 准备    →  创建 workspace + 生成评测计划 + 建立基线
Phase 1: 回顾    →  读取记忆（results.tsv + experiments.jsonl + git log）
Phase 2: 构思    →  分析失败模式，决定改什么
Phase 3: 修改    →  对 skill 做一个原子改动
Phase 4: 提交    →  Git commit
Phase 5: 验证    →  三层测评流水线（Quick Gate / Dev Eval / Strict Eval）
Phase 6: 门控    →  多维度门控决策：保留 / 丢弃 / 回滚
Phase 7: 记录    →  写入实验记忆
Phase 8: 循环    →  继续、升层、或停止
```

### 三层测评（Phase 5 详解）

Phase 5 不是单次评测，是一个三层流水线——便宜的层每轮都跑，贵的层只在必要时跑。

**名称对照——L1 / L2 / L3 和 Quick Gate / Dev Eval / Strict Eval 指的是同一个东西。** `L*` 标签来自脚本文件名（`run_l1_gate.py`、`run_l2_eval.py`），代码和协议里都是这个叫法；`Quick Gate / Dev Eval / Strict Eval` 是文档里的概念名。两套叫法完全对应，哪个读起来更清楚就用哪个。

| 标签 | 又叫 | 检查什么 | 速度 | 什么时候跑 | 脚本 |
|---|---|---|---|---|---|
| **L1** | **Quick Gate** | YAML frontmatter 语法、SKILL.md 有正文、目录结构、Creator 的 `quick_validate.py`、GT 文件结构（prompt + assertions 齐全） | **秒级** | **每轮必跑** — 门卫。失败直接跳到 discard，L2 不跑 | `scripts/run_l1_gate.py` |
| **L2** | **Dev Eval** | 所有 `dev` split GT case 逐条断言打分。6 个纯程序类型（`contains` / `not_contains` / `regex` / `file_exists` / `json_schema` / `script_check`）由 Python 代码评分；2 个语义类型（`path_hit` / `fact_coverage`）由 `BinaryLLMJudge` 评分——LLM 只答 YES/NO，程序汇总。结果：`pass_rate = 通过断言数 / 总断言数` | **分钟级** | **每轮跑**（或 `evolve_plan.md` 指定的频率）— Phase 6 门控决策的主信号 | `scripts/run_l2_eval.py` + `scripts/evaluators.py` |
| **L3** | **Strict Eval** | `holdout` split（过拟合检测，**绝不暴露给 proposer**，Anti-Goodhart 原则）+ `regression` split（防止已有能力被改坏）+ 可选的盲评 A/B 对比（读 Creator 的 `agents/comparator.md`） | **~10 分钟** | **条件触发** — 由 `evolve_plan.md` 定义：每 N 轮 / dev pass_rate 超过阈值 / layer 晋升前。不是每轮都跑 | 没有专用脚本 — Claude 复用 `run_l2_eval.py` + 换 split（`holdout` / `regression`）编排 |

**Fail-fast 原则**：L1（Quick Gate）失败就直接 discard，L2（Dev Eval）根本不会跑。烂改动的成本被压到最低。

**自适应阈值**：样本数、各层频率、重点断言类型、通过阈值**全部 per-skill**，不写死。都在 `<workspace>/evolve/evolve_plan.md` 里，由 Claude 在 Phase 0 根据 skill 类型、GT 数据量、assertion 分布生成。客服 QA skill 的阈值和代码生成 skill 的阈值是不一样的。模板见 `references/eval_strategy.md`。

### 四层架构

```
┌──────────────────────────────────────────────┐
│  第 4 层：搜索（AutoResearch 外循环）          │
│  决定改什么、怎么改                           │
├──────────────────────────────────────────────┤
│  第 3 层：门控（多维度决策）                    │
│  AND 逻辑：质量 + 触发 + 成本 +               │
│  时延 + 回归 必须全部通过                      │
├──────────────────────────────────────────────┤
│  第 2 层：评测（自适应评测引擎）                │
│  快速门控 → 开发集评测 → 严格评测               │
│  策略 per-skill 在 evolve_plan 中定义          │
├──────────────────────────────────────────────┤
│  第 1 层：记忆（结构化实验日志）                │
│  results.tsv + experiments.jsonl +            │
│  git 历史 + best_versions 快照                │
└──────────────────────────────────────────────┘
```

### 分层变异策略

Evolver 按层级递进修改 skill：

| 层级 | 目标 | 成本 | 示例 |
|---|---|---|---|
| Layer 1 | `description` 字段 | 低 | 提升触发准确率（F1） |
| Layer 2 | SKILL.md 正文 | 中 | 改进指令、示例、约束 |
| Layer 3 | scripts/ 和 references/ | 高 | 新增辅助脚本、优化协议 |

**规则：当前层改不动了才升级到下一层。** 单次迭代不允许跨层修改。

---

## 五个模式

> **Evolve 是整个工具存在的原因，其他 4 个模式都是为它服务的。**

| 模式 | 命令 | 功能 | 调用 Creator？ |
|---|---|---|---|
| ⭐ **Evolve**（核心） | `/skill-evolver evolve` | **自主优化循环，无人值守**——8 阶段 loop 自动跑到收敛或最大迭代数，`keep/discard/revert` 都是真的执行 | 部分 |
| Eval | `/skill-evolver eval` | 单次评测，输出 benchmark 报告 | 是 |
| Create | `/skill-evolver create` | 从需求 + GT 生成新 skill | 是 |
| Improve | `/skill-evolver improve` | 人主导定向改进（人决定改什么，Evolver 给 trace 诊断证据并执行） | 是 |
| Benchmark | `/skill-evolver benchmark` | A/B 盲评对比两个版本 | 是 |

模式串联：

```bash
/skill-evolver pipeline ./my-skill/ --mode create+eval+evolve
```

---

## GT（Ground Truth）数据格式

GT 是进化的燃料。没有 GT，不会开始优化。

```json
{
  "id": 1,
  "prompt": "用户对 skill 的输入",
  "assertions": [
    {"type": "contains", "value": "预期输出", "description": "必须包含 X"}
  ],
  "split": "dev",
  "metadata": {}
}
```

### 断言类型

| 类型 | 说明 |
|---|---|
| `contains` | 输出必须包含该文本 |
| `not_contains` | 输出不能包含该文本 |
| `regex` | 输出匹配正则表达式 |
| `path_hit` | 引用了正确的文档路径 |
| `fact_coverage` | 覆盖了指定的关键事实 |
| `script_check` | 自定义脚本返回 pass/fail |
| `json_schema` | 输出符合 JSON schema |
| `file_exists` | 生成了指定文件 |

### 数据分组

每条 GT 必须标注 `split` 字段：

| Split | 用途 | 使用时机 |
|---|---|---|
| `dev` | 主要优化目标 | 每轮 |
| `holdout` | 过拟合检测 | 定期 + 最终 |
| `regression` | 防止能力退化 | 每次门控 |

**没有 GT 数据？** Evolver 会调用 skill-creator 的测试用例生成能力自动构造。

---

## Workspace 结构

Evolver 自身目录**不存储任何** skill 特定数据。所有数据放在目标 skill 的 workspace 中——和 skill 本体**平级**：

```
your-skill/                         # 你的 skill（git 管理）
├── SKILL.md
├── references/
└── scripts/

your-skill-workspace/               # 和 skill 平级，Creator 和 Evolver 共享
├── evals/
│   └── evals.json                  # GT 数据
├── iteration-1/                    # Creator 的评测迭代
└── evolve/                         # Evolver 专属子目录
    ├── evolve_plan.md              # 自适应评测策略
    ├── results.tsv                 # 实验日志（每轮一行）
    ├── experiments.jsonl           # 细粒度记忆
    ├── best_versions/              # 最优版本快照
    ├── iteration-E1/
    │   ├── meta.json               # 迭代元数据 + 聚合快照
    │   └── cases/case_*.json       # per-case 结构化 trace（Meta-Harness）
    └── summary.md                  # 最终进化报告
```

---

## 安装

### 前置条件

| 要求 | 是否必需 | 用途 |
|---|---|---|
| **Python 3.10+** | 必需 | 运行评测脚本 |
| **Git** | 必需 | 跟踪 workspace 改动，支持 keep/discard/revert |
| **skill-creator** | **必需（硬依赖）** | 提供 quick_validate、eval-viewer、grader/comparator 协议 |
| **Claude Code CLI** | 语义断言需要 | LLM 二元分类（path_hit / fact_coverage）；纯程序断言无需此项 |

### 安装 skill-creator（硬依赖）

skill-creator **必须安装**。找不到时 Evolver 启动就报错并给出安装指引。三种安装方式：

**方式 1：插件市场（推荐）**
```
在 Claude Code 里执行: /install skill-creator
```

**方式 2：从 GitHub 手动安装**
```bash
git clone https://github.com/anthropics/skills.git /tmp/anthropic-skills-latest
cp -r /tmp/anthropic-skills-latest/skills/skill-creator ~/.claude/skills/skill-creator
```
源地址：https://github.com/anthropics/skills/tree/main/skills/skill-creator

**方式 3：自定义路径**
```bash
export SKILL_CREATOR_PATH=/your/path/to/skill-creator
# 或通过 CLI 参数：
python3 scripts/evolve_loop.py ./my-skill --gt ./evals.json --run --creator-path /your/path
```

**路径搜索顺序**（`scripts/common.py:find_creator_path`）：
1. `$SKILL_CREATOR_PATH` 环境变量
2. `~/.claude/plugins/marketplaces/*/plugins/skill-creator/skills/skill-creator/`
3. `~/.claude/skills/skill-creator/`
4. `.claude/skills/skill-creator/`
5. `/tmp/anthropic-skills-latest/skills/skill-creator/`

如果都找不到，`require_creator()` 抛出 `CreatorNotFoundError` 并显示安装指引。**没有静默 fallback。**

### 方式 A：作为 Claude Code Plugin 安装（推荐）

```bash
# 直接 clone 到 plugins 目录
cd ~/.claude/plugins
git clone https://github.com/serriezhang/skill-evolver.git
```

重启 Claude Code 即可。

**原理**：仓库根目录的 `.claude-plugin/marketplace.json` 告诉 Claude Code 去 `./plugin/` 里找实际的 skill 文件。和 [claude-mem](https://github.com/thedotmack/claude-mem) 使用完全相同的模式。

### 方式 B：项目级安装

```bash
# 把 skill 文件复制到项目的 .claude 目录
mkdir -p .claude/skills/skill-evolver
cp -R plugin/skills/skill-evolver/* .claude/skills/skill-evolver/
```

### 方式 C：全局 skill 安装

```bash
mkdir -p ~/.claude/skills/skill-evolver
cp -R plugin/skills/skill-evolver/* ~/.claude/skills/skill-evolver/
```

通过 slash command 调用：`/skill-evolver evolve ./my-skill/`

---

## 仓库结构

```
skill-evolver/                              # GitHub 仓库根目录
├── .claude-plugin/
│   ├── marketplace.json                    # Plugin 注册（source → ./plugin）
│   └── plugin.json                         # 根身份证
├── plugin/                                 # Claude Code 实际加载的内容
│   ├── .claude-plugin/
│   │   └── plugin.json                     # Plugin 身份证
│   └── skills/
│       └── skill-evolver/
│           ├── SKILL.md                    # 主 skill 入口（~320 行）
│           ├── references/                 # 6 个参考文档
│           ├── agents/                     # 4 个 agent 协议
│           └── scripts/                    # 7 个 Python 脚本
├── docs/                                   # 技术文档（给人看的）
│   ├── architecture.md                     # 完整技术架构（中文）
│   └── architecture.en.md                  # 完整技术架构（英文）
├── scripts/                                # 构建和同步脚本
│   ├── sync-opencode.sh                    # 从 plugin/ 生成 .opencode/
│   ├── sync-codex.sh                       # 从 plugin/ 生成 .agents/
│   └── sync-all.sh                         # 一次生成两个平台
├── docs/
│   └── README_CN.md                        # 本文件
├── README.md                               # English
└── LICENSE

# 注：.agents/ 和 .opencode/ 平台镜像不进 git,由 sync 脚本按需生成
#     首次 clone 后,Codex/OpenCode 用户各自跑一次对应的 sync 脚本即可
```

---

## 实战示例：用自己迭代自己（baseline 88.9% → 100%）

这是 skill-evolver 用**自己的真框架**（`evaluators.py` + workspace git + Meta-Trace 诊断）迭代自己的最新结果。展示了真实的 keep/discard/revert 三态决策：

```
基线：88.9%（36 个断言中通过 32 个，5/8 cases）

迭代 1：添加 Installing skill-creator 段落到 SKILL.md
  - 诊断：trace 显示 case 3 失败因为 GitHub URL 和 /install 命令
         只在 common.py 运行时错误中存在，markdown 文档里没有
  - 改动：在 SKILL.md Prerequisites 后加可见的 install 段落
  - 结果：94.4%（34/36）← delta +5.6%
  - 门控：KEEP ✅（quality OK: 0.944 ≥ 0.889 + 0.05）
  - Workspace git: 7a35129

迭代 2：添加 eval viewer 步骤到 Evolve 模式流程
  - 诊断：case 8 失败因为 eval viewer 在代码里调用了
         (evolve_loop.py:918-921) 但 markdown 没有提
  - 改动：在 Evolve 模式 step 6 加 eval viewer 说明
  - 结果：97.2%（35/36）← delta +2.8%
  - 门控：DISCARD ❌（delta 0.028 < min_delta 0.05）
  - 动作：git revert HEAD --no-edit（在 workspace git 里）

迭代 3：bundled 文档对齐（anti-patterns 小写 + do-not-guess + eval viewer）
  - 诊断：case 7 + case 8 都是同一根因——markdown 没和代码/协议对齐
  - 改动：lowercase 反模式条目 + 加 "do not guess" Meta-Trace 禁令
         + 重新加回 eval viewer step 6
  - 结果：100%（36/36）← delta +5.6%
  - 门控：KEEP ✅
  - Workspace git: f9e5bce

最终：88.9% → 100%，3 轮迭代（2 keep + 1 discard），
      所有 commits 都在 workspace git，零项目 git 污染
```

**关键证据**：
- 真用了 Creator：L1 gate 调用 `creator/scripts/quick_validate.py`，eval viewer 调用 `creator/eval-viewer/generate_review.py` 渲染了 97KB HTML
- 真用了框架：评测走 `LocalEvaluator._evaluate_assertion()` 而不是 ad-hoc 脚本
- 真用了 git 试错：iter 2 真的被 discard 并 `git revert`，不是改了就不管
- 真用了 Meta-Trace：每个 case 写 trace 文件，Phase 2 cite trace 证据后才提改动


---

## 配置

### 门控阈值

门控阈值在 `evolve_plan.md` 中 per-skill 定义（setup 阶段自动生成）：

```yaml
gates:
  min_delta: 0.02          # 最小提升幅度
  max_regression: 0.05     # 最大回归容忍度
  max_tokens: 50000        # 单次评测 token 预算
  holdout_floor: 0.60      # holdout 最低分数
```

### 清理

```bash
# 清理旧的评测迭代（保留最近 5 轮 + 所有 keep 轮）
python3 scripts/evolve_loop.py ./my-skill/ --cleanup

# 清理旧的最优版本快照（保留最近 3 个）
python3 scripts/evolve_loop.py ./my-skill/ --cleanup-versions
```

---

## 技术文档

- **[技术架构（中文）](./docs/architecture.md)** — 完整技术设计：四层架构、三大支柱、Creator 硬依赖、workspace 设计、协议细节
- **[Technical Architecture (English)](./docs/architecture.en.md)** — Same content, English version

---

## 与 Skill Creator 的关系

Skill Evolver 是 Skill Creator 的**超集**。

| 能力 | Creator | Evolver | 关系 |
|---|---|---|---|
| 创建 skill | 支持 | 支持 | 调用 Creator |
| 评测 skill | 支持 | 支持 | 调用 Creator |
| 改进 skill | 手动 | **自动** | Evolver 核心循环 |
| A/B 评测 | 支持 | 支持 | 调用 Creator |
| 多门控质控 | 无 | **支持** | Evolver 独有 |
| 实验记忆 | 无 | **支持** | Evolver 独有 |
| 自主进化循环 | 无 | **支持** | Evolver 独有 |

---

## 跨平台支持

Claude Code 是主开发平台（source of truth 在 `plugin/` 目录），其他平台通过 sync 脚本自动生成。

| 平台 | 状态 | 目录 | 差异 |
|---|---|---|---|
| Claude Code | ✅ 完整支持 | `plugin/` | 原版 |
| OpenCode | ✅ 已生成 | `.opencode/` | `AskUserQuestion` → `question`，`claude -p` → `llm run` |
| Codex | ✅ 已生成 | `.agents/` | `AskUserQuestion` → 直接提示，`claude -p` → `codex exec` |

**修改 skill 后同步所有平台：**

```bash
bash scripts/sync-all.sh
```

> **注意**：OpenCode 和 Codex 版本已生成但尚未经过实际测试。Claude Code 版本是经过自举测试验证的。

---

## 贡献

1. Fork 本仓库
2. 创建特性分支：`git checkout -b feature/amazing-feature`
3. 运行自举测试验证无破坏：
   ```bash
   python3 scripts/evolve_loop.py ./plugin/skills/skill-evolver/ --gt ./evals.json --run --max-iterations 5
   ```
4. 提交更改
5. 开 Pull Request

---

## 许可证

MIT

---

## 致谢

- **[skill-creator](https://github.com/anthropics/skills/tree/main/skills/skill-creator)** by Anthropic — 评测引擎本身。硬依赖，引用调用，不复制。Creator 更新后 Evolver 自动受益。
- **[AutoResearch](https://github.com/uditgoenka/autoresearch)** — Karpathy 启发的自主迭代外循环，演化为 Evolver 的 8 阶段 loop（含真实 keep/discard/revert，不是改了就不管）
- **Meta-Harness** — Meta 的 agent-optimization 执行轨迹诊断思想。Evolver 的 active diagnosis 协议要求每次提改动前必须 cite 具体 trace 证据（`iteration-E{N}/cases/case_*.json`）。由 `LocalEvaluator.full_eval(..., cases_dir=...)` 自动落盘。
- **ServiceClaw QA V2** — "LLM 二元分类 + 程序算分" 评测哲学的灵感来源
- 为 [Claude Code](https://claude.com/claude-code) 生态系统而建
