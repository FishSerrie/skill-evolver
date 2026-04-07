# 自适应评测策略

> 本文档替代原 `eval_levels.md`。不再使用固定的 L1/L2/L3 分级，而是 per-skill 生成自适应的 `evolve_plan.md`。

---

## 核心理念

每个 skill 的特征不同（类型、GT 数据量、assertion 类型分布），评测策略不应写死。Evolver 在开始优化前，由 Claude 分析目标 skill 的特征，生成 `evolve_plan.md`。

---

## evolve_plan.md 生成过程

### 输入

1. **目标 skill 的 SKILL.md**：识别 skill 类型（客服/代码生成/文档处理/...）和复杂度
2. **GT 数据**：统计 assertion 类型分布、数据量、dev/holdout/regression split 比例
3. **已有评测结果**（如果存在）：识别当前瓶颈

### 分析维度

| 维度 | 分析方法 | 影响决策 |
|---|---|---|
| skill 类型 | 读 SKILL.md description + body | 评测重点（哪些 assertion 类型更重要） |
| GT 数据量 | 统计 dev/holdout/regression 各多少条 | 门控阈值（数据少→放宽 min_delta） |
| assertion 分布 | 统计 contains/fact_coverage/script_check 等比例 | 优化优先级 |
| 当前 trigger F1 | 如果有 trigger eval 历史 | 是否跳过 Layer 1 |
| 当前 pass_rate | 如果有 behavior eval 历史 | 起始 layer 和策略 |

---

## evolve_plan.md 模板

```markdown
# Evolve Plan for: <skill-name>

## Skill 分析
- 类型：<客服问答 / 代码生成 / 文档处理 / ...>
- 复杂度：<低/中/高>
- GT 数据量：dev <N> 条, holdout <N> 条, regression <N> 条
- 关键 assertion 类型：<fact_coverage, path_hit, ...>

## 评测策略

### Quick Gate（每轮必跑）
- YAML frontmatter 语法检查
- trigger 抽样 <N> 条（should_trigger + should_not_trigger）
- hard assertion 抽样 <N> 条核心 dev case

### Dev Eval（频率：<每轮/每 N 轮>）
- 跑 dev split 全部 <N> 条
- 重点关注：<assertion 类型>
- 调用 Creator 的 grader 协议打分

### Strict Eval（触发条件）
- 每 <N> 轮自动触发
- 或 dev pass_rate 超过 baseline + <X>% 时触发
- 跑 holdout <N> 条 + regression <N> 条

## 优化优先级
1. Layer <1/2/3>：<原因>
2. 重点改进：<具体方向>
3. ...

## 门控阈值
- min_delta: <value>（原因）
- trigger_tolerance: <value>
- max_token_increase: <value>
- regression_tolerance: <value>

## 终止条件
- max_iterations: <N>
- stuck_threshold: <连续 N 轮 discard>
- exhaustion: <3 层都尝试后无提升>
```

---

## 不同 skill 类型的策略示例

### 客服/知识库问答 skill

```markdown
## 评测策略
### Quick Gate
- trigger 抽样 5 条
- hard assertion：抽 2 条 fact_coverage 类 case

### Dev Eval（每轮）
- 跑 dev split 15 条
- 重点关注：fact_coverage（覆盖率）、path_hit（检索准确率）

## 优化优先级
1. Layer 2 (Body)：trigger 已达 0.95，跳过 Layer 1
2. 重点：改进跨分类检索规则

## 门控阈值
- min_delta: 0.03（GT 15 条，放宽阈值避免噪声）
- max_token_increase: 0.25（回答较长，放宽）
```

### 代码生成 skill

```markdown
## 评测策略
### Quick Gate
- trigger 抽样 3 条
- hard assertion：跑 1 条 script_check 类 case

### Dev Eval（每轮）
- 跑 dev split 8 条
- 重点关注：script_check（代码正确性）、file_exists（输出完整性）

## 优化优先级
1. Layer 3 (Scripts)：核心能力在辅助脚本
2. 重点：改进代码模板

## 门控阈值
- min_delta: 0.05（代码正确性二值性强）
- max_token_increase: 0.15（代码应精简）
```

### 文档处理 skill

```markdown
## 评测策略
### Quick Gate
- trigger 抽样 3 条
- hard assertion：跑 1 条 file_exists 类 case

### Dev Eval（每轮）
- 跑 dev split 6 条
- 重点关注：file_exists（生成了文件）、json_schema（输出格式）

## 优化优先级
1. Layer 3 (Scripts)：文档处理依赖脚本
2. Layer 2 (Body)：改进指令清晰度

## 门控阈值
- min_delta: 0.02
- max_token_increase: 0.30（文档处理 token 本身较高）
```

---

## Plan 刷新时机

- **首次 evolve**：必须生成
- **Layer promotion 时**：刷新优化优先级和评测策略
- **连续 5 轮 discard 时（stuck）**：重新分析失败模式，刷新策略
- **人工干预时**：用户明确要求调整
