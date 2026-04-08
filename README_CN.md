[English](README.md) | [中文](README_CN.md) | [技术架构](docs/architecture.md)

# Skill Evolver

**Claude Code Skill 自动进化引擎。** 给它一个 skill + 测试数据，它会通过门控评测循环自动迭代优化 skill——无需人工干预。

**三大核心支柱**（缺一不可）：

| 支柱 | 来源 | 提供什么 |
|---|---|---|
| **Creator**（核心评测能力） | [skill-creator](https://github.com/anthropics/skills/tree/main/skills/skill-creator) — Anthropic 官方 skill | 评测、打分、对比协议；HTML eval viewer；快速校验 |
| **AutoResearch**（迭代方法论） | [AutoResearch](https://github.com/uditgoenka/autoresearch) — Karpathy 自主迭代思想 | 8 阶段外循环：搜索 → 修改 → 验证 → 门控 → keep/discard → 循环 |
| **Meta-Harness Trace**（诊断方式） | Meta 的执行轨迹诊断模式 | 每个 case 存完整 trace；每次提改动前必须 cite trace 证据；反事实诊断 |

**Creator 是硬依赖**——找不到就报错并给安装指引，不静默降级。Evolver 通过引用调用 Creator 的能力，**不复制**。Creator 更新后 Evolver 自动受益。

```
          ┌─────────────┐
          │  你的 Skill   │
          └──────┬───────┘
                 │
    ┌────────────▼────────────┐
    │     Skill Evolver       │
    │                         │
    │  搜索 → 修改 →           │
    │  评测 → 门控 →           │
    │  保留/丢弃 → 循环        │
    └────────────┬────────────┘
                 │
          ┌──────▼───────┐
          │  更好的 Skill  │
          └──────────────┘
```

---

## 为什么需要 Skill Evolver？

**skill-creator** 可以创建和评测 skill——但改进是手动的。你看评测结果，决定改什么，编辑，再评测，如此反复。

**skill-evolver** 把这整个外循环自动化了：

| | skill-creator | skill-evolver |
|---|---|---|
| 创建 skill | 支持 | 支持（调用 creator） |
| 评测 skill | 支持 | 支持（调用 creator） |
| 改进 skill | 手动 | **自动** |
| 多门控质量控制 | 无 | **支持** |
| 实验记忆 | 无 | **支持** |
| A/B 对比评测 | 支持 | 支持（调用 creator） |
| 自主进化循环 | 无 | **核心价值** |

**Evolver 调用 Creator，不复制。** Creator 更新后，Evolver 自动受益。

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
Phase 5: 验证    →  快速门控（秒级）+ 开发集评测（分钟级）
Phase 6: 门控    →  多维度门控决策：保留 / 丢弃 / 回滚
Phase 7: 记录    →  写入实验记忆
Phase 8: 循环    →  继续、升层、或停止
```

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

| 模式 | 命令 | 功能 | 调用 Creator？ |
|---|---|---|---|
| **Create** | `/skill-evolver create` | 从需求 + GT 生成新 skill | 是 |
| **Eval** | `/skill-evolver eval` | 单次评测，输出 benchmark 报告 | 是 |
| **Improve** | `/skill-evolver improve` | 人主导定向改进 | 是 |
| **Benchmark** | `/skill-evolver benchmark` | A/B 盲评对比 | 是 |
| **Evolve** | `/skill-evolver evolve` | **自主优化循环** | 部分 |

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

Evolver 自身目录**不存储任何** skill 特定数据。所有数据放在目标 skill 的 workspace 中：

```
your-project/
├── my-skill/                       # 你的 skill（git 管理）
│   ├── SKILL.md
│   ├── references/
│   └── scripts/
└── my-skill-workspace/             # 共享 workspace（Creator + Evolver）
    ├── evals/
    │   └── evals.json              # GT 数据
    ├── iteration-1/                # Creator 的评测迭代
    └── evolve/                     # Evolver 专属数据
        ├── evolve_plan.md          # 自适应评测策略
        ├── results.tsv             # 实验日志（每轮一行）
        ├── experiments.jsonl       # 细粒度记忆
        ├── best_versions/          # 最优版本快照
        └── summary.md              # 最终进化报告
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
├── .opencode/                              # OpenCode 平台变体（自动生成）
│   └── skills/skill-evolver/               # 同一 skill，平台适配
├── .agents/                                # Codex 平台变体（自动生成）
│   └── skills/skill-evolver/               # 同一 skill，平台适配
├── scripts/                                # 构建和同步脚本
│   ├── sync-opencode.sh                    # Claude → OpenCode 同步
│   ├── sync-codex.sh                       # Claude → Codex 同步
│   └── sync-all.sh                         # 同步所有平台
├── README.md                               # English
├── README_CN.md                            # 中文
└── LICENSE
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
| Codex | ✅ 已生成 | `.agents/` | `AskUserQuestion` → 直接提示，`claude -p` → `codex -q` |

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
- **[Meta-Harness](https://arxiv.org/abs/2506.xxxxx)** — 执行轨迹诊断思想，Evolver 的 active diagnosis 协议要求每次提改动前必须 cite trace 证据
- **ServiceClaw QA V2** — "LLM 二元分类 + 程序算分" 评测哲学的灵感来源
- 为 [Claude Code](https://claude.com/claude-code) 生态系统而建
