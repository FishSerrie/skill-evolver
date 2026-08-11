# AutoPrompt 升级调研与方案

> 调研日期：2026-08-10 ｜ 分支：`feat/autoprompt-research`
> 目的：把 skill-evolver 从"优化 Claude Skill"升级为通用**指令/提示词优化引擎**，同时保留 skill 优化能力。

---

## 一、AutoPrompt 的基本原理

自2025 年起，至少三篇系统性survey（arXiv:2502.16923收录于 EMNLP 2025、arXiv:2502.18746）把 APO 形式化为一个**优化问题**，只有三个组件：

| 组件 | 含义 | 在我们这里对应什么 |
|---|---|---|
| **搜索空间** | 候选 prompt / 指令 / few-shot 示例 | SKILL.md 正文、references/、或一段裸 prompt |
| **目标函数** | 在 eval 集上的可量化分数 | GT 断言通过率 + 多门禁 |
| **更新方向** | 怎么从当前 prompt 走到更好的 | Phase 2 诊断 → Phase 3 变异 |

**所有方法的差别只在"更新方向怎么来"。** 这是理解整个领域的关键。

---

## 二、五个方法家族

### 1. 采样 / 坐标式（2023，奠基）
- **APE**（ICLR 2023）：LLM 生成候选指令池 → score function 选最佳。首次把"指令"当程序来搜。
- **OPRO**（ICLR 2024）：让 optimizer LLM 看"历史解 + 分数"轨迹，直接生成新 prompt。GSM8K 超人工 8%，BBH 最高 50%。
- 特点：简单通用，但**靠盲试，样本效率低**。

### 2. 文本梯度式（2024）
- **ProTeGi**：LLM 反思失败案例 → 产出自然语言"伪梯度" → 定向编辑 + beam search。
- **TextGrad**：模拟反向传播，把"梯度"沿计算图传递。
- ⚠️ **重要反向证据**（arXiv:2512.13598）：消融实验显示**梯度类比站不住脚**——三步梯度式 vs 一步直接改写没有稳定优势，甚至**故意用错误的评估函数也不掉分**。
- **结论：不要为形式上的"梯度"增加架构复杂度。** 我们现有的"诊断→修改"两步已足够。

### 3. 进化式（2024）
- **EvoPrompt**：GA /差分进化，prompt 当个体做种群进化。
- **PromptBreeder**：自指进化——连"变异 prompt 本身"也进化。
- 特点：**prompt 空间复杂时（长指令、多约束），种群多样性能避免局部最优**。

### 4. 编译 / 结构式（2024）
- **DSPy / MIPROv2**（arXiv:2406.11695）：把 prompt 当 program 的可学参数，Bayesian search **联合优化 instruction + few-shot 示例**。
- **关键发现（对我们极其重要）**：联合优化比单独优化指令平均高 5-15%。一般来说 **demo 收益 > instruction**，**但在条件规则多的任务上 instruction 反超（14.6 vs 10.4）**。
- → **我们面向的业务规则场景恰好属于后者**，所以优化指令本身是对的方向。

### 5. 反思式进化（2025-2026，当前最优）
**GEPA**（arXiv:2507.19457，UC Berkeley/Stanford，**ICLR 2026 Oral**）：

核心批判：RL（GRPO）把整条执行轨迹压成**一个标量 reward**，丢掉了海量信息。模型知道自己得了 0.4 分，但**不知道为什么**。

GEPA 的三个机制：
1. **自然语言反思而非标量**：保留完整 trace（输入、推理、工具调用、返回、失败点），交给 reflection LM诊断
2. **反馈函数而非单一metric**：metric 同时返回分数 **和文本解释**（编译错误、检索到哪些文档、哪一阶段崩了）。*"反馈函数的质量是GEPA 表现的最大杠杆"*
3. **Pareto 前沿而非单一最优**：保留"在至少一个实例上最好"的候选集合，防止过早收敛

实测：**超 GRPO 平均 ~6pp（最高 20pp），rollout 少 4-35 倍；超 MIPROv2 10%+，prompt 短 9.2 倍**。已集成为 `dspy.GEPA`，独立库 `pip install gepa`。

---

## 三、几个关键技术问题的答案

### Q: 如何避免过拟合 dev 集？
- GEPA 的 **Pareto per-instance 选择**（不只保留平均最优）
- **minibatch 评估**（DSPy 默认仅 3 例）大幅降成本
- 我们已有的 **holdout 反Goodhart 硬否决**（`gate.py`）方向正确，要保留

### Q: 多模型泛化怎么做？
**"Model Drifting"是已被命名的真实现象**：
- 在 GPT-4o 上优化的 prompt 迁到 Claude-3，**性能可掉 30%+**
- **PromptBridge**（arXiv 2025）：两阶段——先在少量对齐任务上分别求源/目标模型最优 prompt，再用Mapping Extractor 蒸馏出"语义差异"，零样本迁移到新任务。Terminal-Bench GPT-4o→o3 提升 39%
- **关键洞察**：**"语义 delta"（prompt 要怎么改才适配某模型）在不同任务间往往是一致的** → 可以学一个通用映射
- **BReAD**（arXiv:2507.09839）正式提出 **CPO（持续提示优化）**：换模型时保留已学到的任务知识，而非从头再来。GPT-3.5→GPT-4o 迁移提升 3.5-16%

→ **这正是你说的"先针对一个模型优化，换模型后接着优化"**，学界已有对应范式，我们的路线是对的。

### Q: 评估成本怎么控？
minibatch + 早停 + 只在关键节点跑全量（我们的 L1 快门/Dev/Strict 三级已是这个思路）

---

## 四、⚠️ 两个必须正视的负面证据

### 1. SkillsBench：模型自写的 skill 可能是负收益

**arXiv:2602.12670（v4, 2026-06）**，87 任务 / 8 领域 / 18 种模型-harness 配置，**已直接核实原文摘要**：

| 条件 | 通过率 |
|---|---|
| 无 skill | 33.9% |
| **人工精编skill** | **50.5%（+16.6pp）** |

且：**"Focused Skills with at most three modules outperform larger or exhaustive bundles"**——不超过 3 个模块的聚焦 skill 优于庞大穷举的包。小模型+skill 能追上大模型无 skill。

（子agent 另报告"模型自写skill 平均 -1.3pp、长文档 -2.9pp"，方向与原文一致但**具体数字我未在摘要中直接核实，引用需谨慎**。）

**对我们的含义**：
- skill-evolver 的全部价值**押在评测闭环的可信度上**。评测不可信 → 自动生成的东西可能不如不生成
- **必须给篇幅设硬门禁**——不能让它越改越长

### 2. Reward Hacking 风险被严重低估

**arXiv:2607.05904**（单作者预印本，结论需谨慎但实验极对症）：
- 无参考 judge 评的是 **plausibility（像不像对的）而非 correctness（对不对）**
- 自博弈把 judge 通过率从 **0.72 推到 0.94，而真实准确率一动不动（0.20）**
- **换更强 judge、跨模型家族 judge、三 judge 严格集成——全部失败**（集成当奖励甚至更糟）
- 唯一有效解：**commit-first / blind-solve**——让 judge **先自己独立作答，再看候选答案**。错误接受率 **0.719 → 0.012**

另有实测：**TextGrad 的 prompt hacking 率高达 86%**；有 agent 直接删掉检测器标记来伪造高分。

其他已确认结论：
- **CheckEval / RaR**：二元 checklist 优于 Likert 打分（方差更小，小模型也能对齐）→ **我们现有的"LLM 只做原子 YES/NO 判断"设计是对的**
- **ICML 2025 Spotlight 立场论文**：几百样本以下**禁止用 CLT**，应改 **Wilson 区间 / Beta-Bernoulli + 配对 bootstrap / McNemar 检验**

---

## 五、我们的独特优势（要明确保护）

**ACE 论文的 context collapse 现象**：单体式反复重写必然退化——实测 9 轮后 prompt 从 1800 token 塌缩到 380 token（brevity bias）。

**skill-evolver 的"一次一个原子修改 + git 可回滚"天然抗这个问题**，这是隐藏优势，不能在升级中丢掉。

同理，现有这些设计与 SOTA 方向一致，应保留：
- Module B 提议者/评审者隔离（靠函数签名物理阻断）≈ GEPA 的反思与评估分离
- Module D 三独立审查员≈ 防 reward hacking 的多正交信号
- holdout 反 Goodhart 硬否决
- 分层变异（避免一次改太多）

---

## 六、现状差距（已实测确认）

| 差距 | 证据 |
|---|---|
| **默认评测器测的是文档措辞，不是执行效果** | `LocalEvaluator._load_skill_corpus()` 拼接 SKILL.md+references 做文本匹配；hello-skill 的失败断言是"SKILL.md 应包含 security" |
| **不支持裸 prompt** | `setup_workspace.py ./my_prompt.txt` → `Error: Skill directory not found` |
| **GT schema 没有 input/expected 概念** | grep `expected_output` 无结果 |
| **无多模型维度** | 一次 evolve 一个 `--model`，无跨模型对比机制 |
| **trigger 门禁默认空转** | `trigger_f1` 默认 1.0，基线也 1.0 → `trigger_ok` 恒 True |

**已有的基础（可复用）**：
- `BehavioralEvaluator`（Module A）已能跑真实 transcript 并按 `target` 字段路由 —— **这是升级的地基**
- `LLM_BACKENDS`已有 `http` 后端（`EVOLVER_LLM_URL`）—— **接混元/任意 API 的现成入口**
- 8 Phase 循环 + 门禁 + git 回滚 + 隔离机制 —— **核心引擎不用推翻**

---

## 七、升级方案（分阶段）

### 阶段 0：概念对齐（无代码，改文档）
把核心抽象从 "skill" 抬升为 **optimization target**：
- `target_type: skill | prompt_file | skill_section`
- 产出物：优化后的文件 + 可导出的纯 prompt 文本

### 阶段 1：让评测测"执行效果"（最高优先级）
1. GT schema 扩展：`input` / `expected` / `rubric`，支持三种判定
   - **程序化**（有标准答案）：exact / regex / json_schema / script —— 最可靠，优先
   - **rubric 二元 checklist**（有规则无标准答案）：拆成原子 YES/NO，程序聚合分数
   - **禁止**裸 Likert 打分和无参考 judge 直接判对错
2. `BehavioralEvaluator` 升为默认（对 prompt 类目标强制）
3. **实现 commit-first judge**：judge 先独立作答再看候选 —— 防 reward hacking 的唯一已验证有效手段

### 阶段 2：裸 prompt 支持
- `prompt_file` 目标类型：单文件也能建 workspace、跑循环、git 版本化
- 变异层重新定义（不再是 description/body/scripts，而是 角色/规则/示例/输出格式）

### 阶段 3：借鉴 GEPA
- **反馈函数**：评测除了分数还返回**文本诊断**（哪条规则没生效、哪个字段错了）
- **Pareto 前沿**：保留"在某些case 上最好"的候选，不只保留平均最优
- 保持我们的原子修改约束（抗 context collapse）

### 阶段 4：多模型
- 走 `http` 后端接入任意 API（混元等）
- **CPO 范式**：换模型时继承已有prompt 继续优化，而非从头
- 产出"通用 prompt" + 差异大时才产"模型专属 prompt"

### 阶段 5：篇幅硬门禁
按 SkillsBench 结论：模块数 ≤3、总长度上限，越改越长直接 discard。

---

## 八、待确认事项

- SkillsBench 的"模型自写 skill -1.3pp"具体数字未直接核实（摘要未提），引用前需查 PDF 正文
- arXiv:2607.05904 是单作者预印本，未经复现
- GEPA PDF 正文未能完整解压，机制描述来自摘要 + 多篇二手解读（互相一致）
- 子 agent 列出的"勿引用"清单：EvoSkill、BinEval、Huxley-Gödel Machine 等未能核实，不得作为依据
