# 分层 Mutation 策略

## 核心原则

**一层改不动才进下一层。不允许跨层改动。**

每轮只允许在当前 layer 内做一个原子改动。

---

## Layer 定义

### Layer 1: Description 层

**改什么**：SKILL.md frontmatter 的 description 字段

**目标**：
- 该触发时触发（recall）
- 不该触发时不触发（precision）

**评什么**：trigger F1

**成本**：低——每轮只需跑 trigger eval（20 条 query，秒级）

**方法**：
- 生成候选 description → 跑 trigger eval → 选最优
- 可复用 skill-creator 的 run_loop 思想（60/40 train/test split）

**进入条件**：默认第一层（如果 trigger 已经很好，可跳过）

**退出条件**：连续 K 轮（默认 5）无 trigger F1 提升

### Layer 2: SKILL.md Body 层

**改什么**：
- 指令措辞和表达方式
- 步骤顺序和流程结构
- 输出格式模板
- 规则的组织和优先级
- 引导语和解释

**目标**：
- 提升回答质量（pass_rate）
- 提升行为稳定性（降低方差）
- 减少无效 token 消耗

**评什么**：behavior GT（assertions pass_rate）

**成本**：中——每轮跑 L2 开发集评测

**方法**：
- 分析失败 case 的共性
- 生成针对性的改进假设
- 做一个原子改动（改一条规则/改一个步骤/改一个模板段落）

**进入条件**：Layer 1 退出后

**退出条件**：连续 K 轮无 pass_rate 提升

### Layer 3: Scripts / References / Resources 层

**改什么**：
- 辅助脚本的逻辑
- reference 文件的内容和结构
- 检索配置和参数
- 模板文件
- 知识库索引

**目标**：
- 提升 skill 能力上限
- 解决 body 层无法解决的结构性问题

**评什么**：全量 behavior + 性能指标

**成本**：高——每轮跑 L2 + 可能触发 L3

**方法**：
- 需要更深的 code-level 分析
- 改动范围可能更大，但仍需保持原子性

**进入条件**：Layer 2 退出后

**退出条件**：连续 K 轮无提升 → 所有 layer 都尝试过，结束 evolve

---

## Layer Promotion 机制

```
当前 layer 连续 K 轮无 keep
  → 输出当前 layer 总结（成功/失败改动统计）
  → 升级到下一 layer
  → 重置连续 discard 计数器

所有 layer 都尝试过且无提升
  → 输出最终报告
  → 结束 evolve
```

---

## 原子改动自检

每次 Modify 后，执行自检：

1. **一句话测试**：能否用一句话描述这个改动？需要用"and"→ 说明是两个改动，拆分。
2. **文件数检查**：`git diff --name-only | wc -l`。超过 3 个文件 → 大概率不是原子改动。
3. **diff 大小检查**：`git diff --stat`。新增行数超过 30 行 → 需要审视是否可以更精简。
