# AutoPrompt 引擎设计

> 分支：`feat/autoprompt-research` ｜ 前置：`docs/autoprompt-research.md`
> 目标：把 evolver 从「skill 优化器」升级为通用**指令/提示词优化引擎**，同时保留 skill 优化能力。

---

## 0. 第一原则：合理的架构

不是抑制代码量，也不是堆代码，而是**用软件工程的既有思想把职责摆对位置**。判据只有一条：

> 每个抽象是否让「变化」被局限在一个地方？

### 应用的软件工程原则

| 原则 | 在本设计中的落点 |
|---|---|
| **单一职责（SRP）** | 「怎么得到判定集合」与「集合怎么变成分数」是两件事，分属`Grader` 与 `scoring` |
| **开闭原则（OCP）** | 新增一种判定方式 = 新增一个 `Grader` 实现，不改引擎、不改 `gate`、不改已有 Grader |
| **里氏替换（LSP）** | 任何 `Grader` 返回的 `Judgment` 对引擎完全等价；引擎不需要 `isinstance` 分支 |
| **依赖倒置（DIP）** | 引擎依赖 `Grader`/`Target` 抽象，不依赖任何具体数据格式或业务字段 |
| **接口隔离（ISP）** | `Target` 只暴露 `read/write/snapshot`；变异逻辑拿不到评测能力，评测拿不到写权限 |
| **组合优于继承** | `Grader` 之间**不**互相继承；共用能力靠组合 `scoring` 与 `judge` 依赖注入 |
| **模板方法（继承的正当用法）** | 三个 Grader 的「取集合 → 送scoring → 组装 Judgment」骨架相同，抽到 `BaseGrader` |
| **多态** | 引擎对Grader / Target 只做多态调用，`kind` 与判定类型都不在引擎里做分支判断 |
| **DRY** | 同一职责只允许一处实现（下方 I1/I2 是可检验的实例） |
| **YAGNI** | 只实现已确认需要的三种判定模式，不为假想需求预留扩展点 |

### 可检验的不变量

抽象是否成立不靠自觉，靠能被 grep 或测试抓住的规则：

| # | 不变量 | 检验方式 |
|---|---|---|
| I1 | 「从 LLM 文本提取 JSON」只有一处实现 | 全仓库只有一个此类函数定义 |
| I2 | 「集合 → 数值」只有一处实现 | `graders.py` 中**不得出现除法运算** |
| I3 | 核心模块不含任何业务/项目特有名词 | grep 项目名、业务字段名在核心模块下为 0 |
| I4 | 引擎不对 Grader / Target 做类型分支 | 引擎代码中无 `isinstance` / `kind ==` 判断 |
| I5 | `scoring` 是纯函数 | 该模块不import LLM、不做文件 IO |
| I6 | 每个新模块只依赖抽象层，不反向依赖引擎 | 其单测只 import 该模块 + stdlib + 被复用的约定层（`common`） |

> **I6 的措辞经修正**：原表述为「只 import 该模块及stdlib」，但 `target.py` 刻意依赖 `common` 的 workspace 约定与 skill 布局枚举——因为「一个约定只有一份定义」优先于「模块零依赖」。零依赖会逼出第二份约定副本，而那正是 I1/DRY 要禁止的。故 I6 约束的是**依赖方向**（不得反向依赖引擎），而非依赖数量。

### 顺带偿还的既存债务

**「从 LLM 文本提取最后一行 JSON」目前有三处独立实现**（`isolation.py` 两处、`verifier_panel.py`一处）。这违反 I1，且新增 Grader 会需要同样能力——若不先收敛，就会出现第四处。

因此**第一步是抽出唯一实现并让三处改为调用**，净减代码。先偿债再加功能。

---

## 1. 问题诊断：缺的是一层抽象，不是功能

现状的耦合点只有两个：

```python
Evaluator.full_eval(skill_path, gt_path, split)
#                   ^^^^^^^^^^ 契约钉死「被优化对象 = 一个 skill 目录」

LocalEvaluator._load_skill_corpus()
#              ^^^^^^^^^^^^^^^^^^ 判定钉死「评的是拼接后的静态文档文本」
```

后果（均已实测确认）：
- 独立的 prompt 文件无法作为优化对象（`setup_workspace` 直接报错）
- 评的是文档措辞而非执行效果
- 没有「换模型」这个维度

**而引擎其余部分（8 Phase 循环、多门禁、git 回滚、提议者/评审者隔离）与「被优化对象是什么」完全无关。**

所以这是一次**依赖倒置**：把两个具体依赖抽成接口，而非重写引擎。

---

## 2. 三个抽象

### 2.1 `Target` —— 被优化的对象

```python
class Target(ABC):
    """被优化对象的抽象。引擎只通过这几个方法接触它。"""
    @abstractmethod
    def read(self) -> str: ...              # 取出可变异文本
    @abstractmethod
    def write(self, text: str) -> None: ... # 写回
    @abstractmethod
    def snapshot(self) -> dict: ...         # 结构指标（统一键集，供门禁用）

    def context(self) -> str:# 送评语料，默认 == read()
        return self.read()

class SkillTarget(Target): ...        # 整个 SKILL.md
class PromptFileTarget(Target): ...   # 任意独立 prompt 文件
class SectionTarget(Target): ...      # 文件内某个小节
```

**为什么用继承而非 `kind` 字段**：三种形态的 `read/write` 行为**本质不同**（整文件 vs 定位小节边界后局部替换）。用 `kind` 字段就会在方法内部写 `if kind == ...` 分支——那是把多态退化成条件判断，违反 I4和 OCP。用子类，引擎只做多态调用。

`snapshot()` 存在的唯一理由：门禁需要知道「改动后是否变得更臃肿」。**注意它返回统一键集**（`SNAPSHOT_KEYS`：`chars/lines/non_empty_lines/child_units/child_lines`），形态特有信息一律进 `extra` 且门禁不读。若各形态返回不同键，门禁就必须写 `if "share_of_file" in snap`——那是**伪装成字典查键的类型分支**，同样违反 I4。

`context()` 与 `read()` 的区分是**必须的，不是预留**：可变异的文本 ≠ 送评的文本。skill 的 reference 文件在运行时被读取，所以必须进评测语料，但一步只允许改`SKILL.md`（否则无法把分数变化归因到某一个文件）。若无此方法，评测器为取语料就只能反问「这是哪种 target」——正是 I4 要消灭的 `isinstance`。

`vcs_root` 是基类的**具体实现**（向上找 `.git`），不是抽象方法：找仓库根对所有形态完全相同，抽象化只会让每个子类重写同一段上行遍历。

### 2.2 `Judgment` —— 评测与引擎之间的唯一契约

```python
@dataclass(frozen=True)
class Judgment:
    case_id: str
    metrics: dict[str, float]   # {"precision":.., "recall":.., "f1":..} 各自独立
    primary: str                # 哪个 metric 参与门禁排名
    passed: bool
    feedback: str               # 自然语言诊断（给反思用）
    evidence: dict              # 集合划分明细 / 逐条断言结果
    cost: dict                  # {"tokens":.., "duration_ms":..}

    @property
    def score(self) -> float:
        return self.metrics[self.primary]
```

**`metrics` 多维而非单标量**：precision 与 recall 诊断的是**不同的失败**——recall 低是漏了要点，precision 低是编了没有的。合成一个数会毁掉诊断阶段最需要的信号。`primary` 让门禁仍有一个可比的排名维度，兼顾两者。

**`feedback` 是本设计最高杠杆的字段**。调研结论：反馈函数质量主导优化器表现。现有评测只回 `pass: bool`，诊断只能猜；有了 `feedback`，诊断能看到「要点 3 未被覆盖」这种可直接行动的信息。

`frozen=True` 是刻意的：判定结果一旦产生不应被下游修改，否则「谁改了分数」将无法追溯。

### 2.3 `Grader` —— 怎么判定「好」

```python
class BaseGrader(ABC):
    """模板方法：固定骨架，子类只实现「怎么得到集合」。"""
    def grade(self, case: dict, output: str) -> Judgment:
        outcome = self._classify(case, output)   # 子类实现
        metrics = scoring.compute(outcome)       #唯一算分实现
        return Judgment(..., metrics=metrics,
                        feedback=self._describe(outcome))

    @abstractmethod
    def _classify(self, case, output) -> Outcome: ...
```

三种实现：

| Grader | 适用场景 | 如何得到集合 | 可靠性 |
|---|---|---|---|
| `ProgrammaticGrader` | 有标准答案且可程序判定 | exact / regex / json_schema / script | ★★★ |
| `PointCoverageGrader` | 有标准答案且已拆成要点 | LLM 划分 matched/missing/extra | ★★★ |
| `RubricGrader` | 只有「什么算好」的规则 | 逐条二元 YES/NO | ★★ |

**模板方法是这里继承的正当用法**：三者的骨架（取集合 → 算分 → 组装）完全相同，差异只在 `_classify`。但**它们之间不互相继承**——`PointCoverageGrader` 不是 `ProgrammaticGrader` 的特例，硬套会造成 LSP 违反。

**无标准答案时的关键防护**：`RubricGrader` 支持 `commit_first=True` —— 评判者先按规则独立作答，再看候选。调研中这是唯一被验证有效的反作弊手段（错误接受率 0.719→0.012），其余手段（换更强模型、跨家族、多评判者集成）全部失败。

**明确不做**：让 LLM 直接给 1-10 分。调研与实践都指向同一结论——LLM 只做分类，程序负责算分。

---

## 3. 吸收的评测思想

四条思想来自一套已在生产中运行的评测体系。**只吸收思想，不复用其代码**——因为那套体系的分数口径是为人类汇报设计的（截尾均值、多预设权重），而本引擎需要的是给算法过阈值用的口径。目标不同，共用实现即错配。

| # | 思想 | 落地方式 |
|---|---|---|
| 1 | **LLM 只做集合划分，分数由程序算** | `Grader._classify` 返回集合，`scoring.compute` 算数值。LLM 无法直接操纵分数 |
| 2 | **集合守恒校验** | ★ 升为引擎级不变量：`matched + partial + missed == expected_total`，不成立则该 case 判 `error` |
| 3 | **交叉校验探针，不参与排名** | `Judgment.evidence["probe"]` 承载健康度信号，不进 `metrics` |
| 4 | **判定标准可切换** | GT 可含多组标准（如「正确性」「表达质量」），由配置选择拟合哪一组或全部；引擎不预设|

第 2 条值得强调，但**必须准确说明它能与不能做什么**——这一点在第 5 步的对抗审查中被纠正过，原表述夸大了它的能力：

**能拦住的**：分类器**漏掉**要点。虚报 matched 就必须从 missed 挪走，守恒等式让"少算"无处可藏。这与引擎既有的「靠函数签名做隔离」同源——**用算术拦住作弊，而不是请求模型别作弊**。

**拦不住的**：分类器**每一条都放了位置、但放错了**。声称 4/4 全中而实际只覆盖 1 条，等式完美成立（4 = 4）。**任何算术都检测不到这种情况**，因为账目上什么都没少。

> **实测证据**：给 4 个要点、模型回 `matched:[1,2,3,4]`、候选实际只覆盖 1 条 → `error: None`，`f1: 1.0`。

因此：
- **守恒校验是地板，不是天花板**。它保证"账目自洽"，不保证"判断正确"。
- **判断正确性只能靠独立意见**——这正是第 8 步 `RubricGrader` 的 commit-first 存在的唯一理由，也是它不可省略的原因。若误以为守恒已解决作弊问题，第 8 步就会被建立在一个不存在的防线上。
- **身份判定属于调用方，不属于 `scoring`**。早期版本还比较"两个桶里是否有相同字符串"，那是错的：GT 合法地可以列两条相同期望，产出项也合法地可以等于某条期望——两者都会让整个 case 被误判为"不一致"。只有构造 `Outcome` 的那一层知道自己的项是位置还是值（见 `PointCoverageGrader` 拒绝重复的**索引**）。

第 4 条按review 意见调整：**两档标准不是核心设计点**，只是「拟合哪一列数据」的配置差异。引擎不为此建专门机制。

### 通用性红线

任何项目名、业务字段名、领域术语**不得进入核心模块**。它们只能作为**参数**出现在数据加载层：

```python
CsvPointsLoader(
    path=...,
    question_col="question",     # 列名是参数
    points_col="...",            # 不是硬编码
    split_col=None,              # 缺失时按比例/分层切分
    stratify_col=None,           # 可选：按类目分层，避免某类全落一侧
)
```

具体某份 GT 只是这个加载器的一个**配置实例**，不是它的特例。

### 分数严格度

面向算法阈值而非人类阅读，因此：

| 决策 | 做法 |
|---|---|
| **部分覆盖** | 计**较低分数**（默认权重 0.5，可配置），而非满分或0 分。部分覆盖确实优于完全遗漏，一律归零会丢失优化梯度 |
| **不建汇报层** | 产出为结构化 `Judgment` + 实验日志 + `feedback` 文本，不生成人类报表 |
| **守恒失败即 error** | 不降级、不猜测，该 case 不计入分数并在 `feedback` 标注 |
| **阈值独立** | `metrics` 各维度可在优化计划中单独设阈值 |

---

## 4. 模块划分

按职责边界划分，每个模块声明它**拥有**什么、**不拥有**什么。

```
scripts/
├── json_extract.py   拥有：从 LLM 文本提取 JSON 的唯一实现
│不拥有：任何业务语义
│                     ← 三处既存重复收敛至此（I1）
│
├── target.py         拥有：Target 抽象 + 三个子类的读/写/结构快照
│                     不拥有：评测、变异决策
│
├── scoring.py        拥有：集合 → 数值的唯一实现 + 守恒校验
│                     纯函数，无 LLM、无 IO、仅 stdlib（I5）
│                     不拥有：怎么得到集合
│
├── judgment.py       拥有：Judgment 契约 + Outcome 集合结构
│                     不拥有：算分、判定
│
├── graders.py        拥有：BaseGrader 模板 + 三个 _classify 实现
│                     不拥有：算分（调scoring）、调模型（注入 judge）
│
├── datasets.py       拥有：外部 GT → 通用 case dict；列名可配置
│                     不拥有：任何具体业务列名
│
├── evaluators.py     [改] 增 GraderEvaluator：串 target→run→grade→aggregate
│                          现有 LocalEvaluator 行为不变（回归保护）
├── gate.py           [改] + 结构门禁（读Target.snapshot）；metrics 多维阈值
├── isolation.py      [改] 改调 json_extract（净减）
└── verifier_panel.py [改] 同上（净减）
```

### 依赖方向（单向无环）

```
json_extract ← 无依赖，谁都可用
scoring      ← 纯函数，仅被 graders 使用
                ↓
judgment ←─── graders ──→ binary_judge / llm（既有，依赖注入）
                  ↑
target ────→ evaluators ──→ gate（既有）
                  ↑
datasets ─────────┘
```

### 为什么这样切（备选方案与否决理由）

| 备选 | 否决理由 |
|---|---|
| Grader 塞进现有 `evaluator_backends.py` | 该文件已 619 行、承载 4 个不相关后端。再塞会形成第二个杂物间，违反 SRP |
| 算分写在各 Grader 内部 | 三个 Grader 各算一遍 P/R/F1 = 三份重复，违反 DRY/I2 |
| `Target` 用 `kind` 字段而非子类 | 会在方法内长出 `if kind ==` 分支，多态退化为条件判断，违反 OCP/I4 |
| 不单独建 `json_extract.py` | 等于默许第四处重复存在，违反 I1 |
| 新建 `autoprompt/` 子包 | 现有脚本平铺，引入子包造成两套组织方式并存 |

---

## 5. 复用清单（既有资产零改动）

| 既有资产 | 在新架构中的角色 |
|---|---|
| 8 Phase 循环| 不变。本就与「被优化对象是什么」无关 |
| 多门禁 + holdout 反 Goodhart 否决 | 不变，仅增结构门禁与多维阈值 |
| 提议者/评审者隔离（诊断与变异分离） | 不变。变异改为经 `Target.write()` 落地 |
| 对抗审查面板（三独立审查员） | 不变。恰是防作弊所需的多正交信号 |
| `BinaryLLMJudge` | **直接复用**——其设计原则已是「只分类不打分」，与本设计同源 |
| `LLM_BACKENDS`（含 `http` 后端） | **直接复用**——接任意模型 API 的现成入口 |
| git 提交/回滚、实验日志与记忆 | 不变 |
| 真实执行 transcript 生成 | 复用为「产出待判定输出」的一种方式 |

**核心引擎一行不改**——新增的只是「被优化对象」与「判定方式」两个维度的抽象。

---

## 6. 多模型

需求：先在一个模型上优化，换模型后**继承已有成果继续**优化，产出通用 prompt +（差异显著时）模型专属版本。

对应调研中的「持续提示优化」范式。实现只需两件事：

1. **模型经 `http` 后端接入**——已有能力，零新代码。
2. **实验日志增`model` 维度**——换模型后从当前最优继续迭代，且能看出某条改动在哪个模型上有效。

「模型迁移会掉分」是已被命名的现象（跨模型可掉 30%+），但**跨模型自动对比、语义差异映射本期不做**（YAGNI）——先让单模型链路完整。

---

## 7. 实施顺序

先补齐全部功能，再按此优先级推进。每步独立可验证、可回滚。

| 步 | 内容 | 验收标准 |
|---|---|---|
| 1 | `json_extract.py` + 收敛三处重复 | 现有 174 测试全绿，代码**净减** |
| 2 | `scoring.py` | 除零 / 空集 / 守恒失败 / 部分覆盖权重四类边界有单测 |
| 3 | `judgment.py` | 可独立构造；`score` 正确取 `primary`；不可变性生效 |
| 4 | `target.py` + 接通 `setup_workspace` | 三个子类均可读写；`setup_workspace <裸prompt文件>` 成功（此前报错）|
| 5 | `graders.py` `ProgrammaticGrader` + `PointCoverageGrader` | 守恒校验能抓出**漏报**（反例测试）；**明确记录它抓不住"放满但放错"**——那一类只能靠第 8 步 commit-first |
| 6 | `datasets.py` | 列名可配置；支持分层切分 |
| 7 | 接入 `evaluators.py` | 现有行为不变（回归）；新路径可选启用 |
| 8 | `RubricGrader` + commit-first | 无标准答案场景可用；commit-first 有对照实验 |
| 9 | 结构门禁 + 多维阈值 | 变臃肿的候选被 discard |

**第 1 步净减代码**——先偿债再加功能。

**第 2、3 步是纯函数与纯数据结构**，无 IO 无 LLM，可做到高分支覆盖。整个评测体系的可信度建立其上，因此必须先于任何涉及 LLM 的代码。

---

## 8. 明确不做（YAGNI）

-❌ 复用或调用外部评测体系的代码（口径目标不同）
- ❌ 人类可读报表层（分数面向算法阈值）
- ❌ textual gradient（调研显示梯度类比经消融不成立）
- ❌ Pareto 多候选前沿（保住单原子修改对context collapse 的抵抗力）
- ❌ few-shot 示例自动挑选（规则密集型任务中指令优化收益更高）
- ❌ 触发率评测（与本主线无关）
- ❌ 重构 `evaluator_backends.py`（避免扩大战场）
- ❌ 跨模型自动对比（待单模型链路完整）
