# Skill-Evolver 技术对比与设计决策分析

> 作者: serriezhang + Claude | 日期: 2026-04-08（Creator 硬依赖重构 + self-iteration 验证后更新）
> 用途: 发布文档、技术博客、README 引用的素材库

---

## 一、Skill-Evolver vs AutoResearch

### 背景

AutoResearch (by uditgoenka, 基于 Karpathy 思想) 是通用的"循环执行器"——给一个 verify 命令返回数字，它不断改代码、跑命令、看数字。

Skill-Evolver 在 AutoResearch 的 loop 骨架上，融合了 Creator 的评测能力和 Meta-Harness 的诊断思想，专门用于 skill 优化。

### 逐项对比

| 维度 | AutoResearch | Skill-Evolver | 优势方 |
|---|---|---|---|
| **核心 loop** | 8 阶段 | 8 阶段 | 持平 |
| **实现方式** | 纯 Markdown 协议 | Markdown + Python 脚本混合 | SE（确定性步骤用脚本更可靠） |
| **评测能力** | 用户提供 verify 命令 | 内置 8 种断言 + BinaryLLMJudge + Creator | **SE 远超** |
| **评测哲学** | 单一数字指标 | LLM 二元分类 + 程序算分（多维度） | **SE** |
| **门控** | 单指标 improve/worse | 5 维 AND（质量+触发+成本+时延+回归） | **SE** |
| **记忆** | git log + results.tsv | git + tsv + jsonl + traces | **SE（有执行轨迹）** |
| **诊断** | 读 git log 推理 | 读 trace 做反事实诊断 | **SE（Meta-Harness）** |
| **层级策略** | 无分层 | 3 层 mutation（desc→body→scripts） | **SE** |
| **GT 构造** | 无（用户必须提供） | 自动从 SKILL.md 生成 | **SE** |
| **平台支持** | Claude/OpenCode/Codex | Claude/OpenCode/Codex/HTTP | 持平 |
| **子命令多样性** | 10 个特化命令 | 5 个模式 | AR（更多场景覆盖） |
| **Creator 集成** | 无 | **硬依赖** + trigger eval + viewer + grader/comparator 指针 | **SE** |
| **防刷分** | 无 | holdout set + 负面断言 + Anti-Goodhart | **SE** |
| **Workspace git 隔离** | 无 | self-iteration 时 commits 落 workspace git，零项目 git 污染 | **SE**（v2.1+） |

### 结论

**AutoResearch = 通用 loop 骨架（优化任何东西）**
**Skill-Evolver = AutoResearch + Creator 评测引擎 + Meta-Harness 诊断大脑（专门优化 skill）**

Skill-Evolver 在评测深度、诊断能力、门控严格度三个核心维度上全面超越 AutoResearch。AutoResearch 在场景覆盖面上更广（10 个子命令 vs 5 个模式）。

---

## 二、Skill-Evolver vs Meta-Harness

### 背景

Meta-Harness (2026 论文) 的核心洞察：给 AI 完整的执行轨迹（千万 token 级），让它自己检索诊断，而不是喂压缩摘要。

### 融入程度

| Meta-Harness 思想 | Skill-Evolver 实现 | 实现级别 |
|---|---|---|
| 全量执行轨迹 | evolve/iteration-E{N}/traces/case_*.md | ✅ 完整实现 |
| AI 主动检索诊断 | Phase 1 读 trace → Phase 2 prompt 包含 trace + 强制诊断协议 | ✅ 完整实现 |
| 反事实分析 | experiments.jsonl 的 diagnosis 字段 + Phase 2 要求 "Case X failed because Y" | ✅ 轻量版实现 |
| 防刷分/Goodhart | holdout set + not_contains 断言 + 结构完整性检查 | ✅ 完整实现 |
| 千万 token 文件系统检索 | 未实现（skill 规模不需要） | ⬜ 有意不做 |

### 差异

Meta-Harness 优化的是完整的 harness（系统提示词+工具定义+重试策略+环境配置）。
Skill-Evolver 优化的是 skill（SKILL.md + references + scripts），通过 3 层 mutation 逐层深入。

本质上是同一思想在不同粒度上的应用。

---

## 三、评测哲学：LLM 二元分类 + 程序算分

### 来源

灵感来自 ServiceClaw 客服 QA 自动评测系统 V2 (serriezhang, 2026-03)。

### 核心原则

| 原则 | 说明 |
|---|---|
| LLM 只做分类 | 每次调用只问一个 YES/NO 问题 |
| 程序做所有计算 | pass_rate = passed_count / total_count |
| 相同分类 → 相同分数 | 确定性保证，消除 LLM 算分漂移 |
| 二元判断更稳定 | 比让 LLM 打 1-5 分或给百分比更可靠 |

### 与 ServiceClaw V2 的类比

| ServiceClaw V2 | Skill-Evolver | 对应关系 |
|---|---|---|
| R1a: matched/missing/incorrect/extra 分类 | assertion: YES/NO 二元判断 | LLM 做分类 |
| P/R/F1 由程序从列表长度算 | pass_rate 由程序从 boolean 列表算 | 程序算分 |
| Phase 0 拆点（离线 GT + 在线 pred） | fact_coverage preset（预置 facts）+ online（LLM 拆） | 拆点策略 |
| R1b 一致性校验 | 无（可作为 v3 功能） | - |
| R3 忠实度门控 | regression gate + holdout gate | 间接对应 |
| 4 维度独立评估 | 单一 pass_rate（但 per-assertion 粒度） | SE 更通用但粒度不同 |

### 8 种断言类型分类

| 类型 | 判断方 | 确定性 |
|---|---|---|
| contains | 程序 | 100% |
| not_contains | 程序 | 100% |
| regex | 程序 | 100% |
| file_exists | 程序 | 100% |
| json_schema | 程序 | 100% |
| script_check | 程序 | 100% |
| path_hit | LLM 二元 | ~95%（二元分类很稳定） |
| fact_coverage | LLM 二元 per fact | ~95% |

---

## 四、三层融合架构

```
┌─────────────────────────────────────────────────────────┐
│  Layer 3: Meta-Harness 思想                              │
│  · Trace 存储（完整执行轨迹，不压缩）                      │
│  · 主动诊断（先 grep trace，再提方案）                     │
│  · 反事实分析（因果推理，不是猜测）                         │
│  · 防刷分（holdout + 负面断言 + Anti-Goodhart）           │
├─────────────────────────────────────────────────────────┤
│  Layer 2: Creator 思想                                    │
│  · LLM 二元分类 + 程序算分                                │
│  · 8 种断言类型（6 程序 + 2 LLM 二元）                     │
│  · GT 自动构造（读 SKILL.md 生成测试用例）                  │
│  · Eval Viewer（HTML 可视化）                             │
│  · Trigger 评测 + 智能 Creator 检测                       │
├─────────────────────────────────────────────────────────┤
│  Layer 1: AutoResearch 思想                               │
│  · 自主 loop（8 阶段无人干预）                              │
│  · 指标驱动搜索（读 memory 决定改什么）                     │
│  · 多门控 AND（5 维全部通过才 keep）                       │
│  · 结构化记忆（tsv + jsonl + git + traces）              │
│  · 卡顿检测 + 层级晋升                                    │
│  · Keep/Discard + git revert（保留审计轨迹）              │
└─────────────────────────────────────────────────────────┘
```

---

## 五、通用性设计

### 不绑定任何特定工具

| 组件 | 可替换性 |
|---|---|
| LLM 后端 | claude/codex/opencode/HTTP（env LLM_BACKEND 切换） |
| Creator | 任何有评测能力的 skill（按 description 语义检测） |
| Evaluator | 4 种内置 + 自定义脚本接口 |
| 平台 | Claude Code / OpenCode / Codex（同步脚本维护） |

### Creator 智能检测

不靠名字匹配 `*-creator`，而是扫描所有已安装 skill 的 SKILL.md description，找含有 eval/grading/benchmark 关键词且有 scripts/ 目录的工具。

---

## 六、与其他方案的信息量对比

引用 Meta-Harness 论文数据：

| 方法 | 提供给优化器的信息量 |
|---|---|
| Self-Refine | ~1,000 tokens（最近一次输出+自我批评） |
| OPRO | ~2,000 tokens（几轮方案+分数） |
| TextGrad / AlphaEvolve / GEPA | 8,000-26,000 tokens |
| Meta-Harness | 最高 10,000,000 tokens（完整文件系统） |
| **Skill-Evolver** | results.tsv + experiments.jsonl + traces + git log（动态量，典型 50,000-200,000 tokens） |

Skill-Evolver 不追求千万 token 级（skill 规模不需要），但远超传统方法的信息量。关键是 trace 文件提供了原始执行轨迹，不是压缩摘要。

---

*此文档供发布文档、技术博客、README 引用使用。*
