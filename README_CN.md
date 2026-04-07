# Skill Evolver

**Claude Code Skill 自动进化引擎。** 给它一个 skill + 测试数据，它会通过门控评测循环自动迭代优化 skill——无需人工干预。

基于 [skill-creator](https://github.com/anthropics/claude-plugins-official/tree/main/plugins/skill-creator) 的评测能力 + [AutoResearch](https://github.com/uditgoenka/autoresearch) 的自主迭代思想构建。

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

- [Claude Code](https://claude.com/claude-code) CLI 已安装
- [skill-creator](https://github.com/anthropics/claude-plugins-official/tree/main/plugins/skill-creator) 插件已安装（Claude Code 默认自带）
- Python 3.10+
- Git（推荐，用于版本追踪）

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
│   ├── architecture-v2.1.md                # 完整技术架构
│   └── bootstrap-report.md                 # 自举测试报告
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

## 实战示例：从 50% 进化到 100%

这是 skill-evolver 自举测试的真实结果（用自己优化自己）：

```
基线：50%（18 个断言中通过 9 个）

第 1 轮迭代：
  - 发现：Evolve 执行指引还在引用旧的手动 bash 工作流
  - 改动：用 11 行自动运行说明替换 27 行手动 bash
  - 结果：100%（18/18）← +50% 提升
  - 门控：保留 ✅
  - Git：+11 -27 行（净减 16 行）

第 2 轮迭代：
  - 所有断言通过，没有更多失败模式
  - 决策：停止（已穷尽）

最终：50% → 100%，2 轮迭代，净减 16 行代码
```

完整报告：[自举测试报告](./docs/bootstrap-report.md)

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

- **[技术架构 v2.1](./docs/architecture-v2.1.md)** — 完整技术设计：四层架构、workspace 设计、协议细节、v1.1 → v2.1 变更日志
- **[自举测试报告](./docs/bootstrap-report.md)** — 自进化测试结果

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

- **[skill-creator](https://github.com/anthropics/claude-plugins-official/tree/main/plugins/skill-creator)** by Anthropic — 为 Evolver 提供评分能力的评测引擎
- **[AutoResearch](https://github.com/uditgoenka/autoresearch)** — 启发了外循环设计的自主迭代模式
- 为 [Claude Code](https://claude.com/claude-code) 生态系统而建
