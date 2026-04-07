# Memory Schema

## results.tsv

AutoResearch 风格实验日志，每轮一行。

### 格式

```
# metric_direction: higher_is_better
iteration<TAB>commit<TAB>metric<TAB>delta<TAB>trigger_f1<TAB>tokens<TAB>guard<TAB>status<TAB>layer<TAB>description
```

### 列定义

| 列 | 类型 | 说明 |
|---|---|---|
| iteration | int | 序号，0=baseline |
| commit | string | git short hash（7字符），discard 时为 "-" |
| metric | float | 主指标值（dev pass_rate，百分比） |
| delta | float | 相对上一次 best 的变化（带正负号） |
| trigger_f1 | float | trigger F1 值 |
| tokens | int | tokens_mean |
| guard | enum | `pass` / `fail` / `-` |
| status | enum | `baseline` / `keep` / `discard` / `crash` / `revert` |
| layer | string | `description` / `body` / `script` / `-` |
| description | string | 一句话描述本轮改动 |

### 示例

```tsv
iteration	commit	metric	delta	trigger_f1	tokens	guard	status	layer	description
0	a1b2c3d	65.0	0.0	0.88	1200	pass	baseline	-	initial baseline
1	b2c3d4e	68.0	+3.0	0.88	1180	pass	keep	body	改进路径检索的易混淆提示
2	-	64.0	-1.0	0.85	1350	fail	discard	body	简化 Pipeline 为两步
3	c3d4e5f	70.0	+2.0	0.90	1190	pass	keep	body	增加跨分类检索指引
```

### 初始化

```bash
echo "# metric_direction: higher_is_better" > <workspace>/evolve/results.tsv
echo -e "iteration\tcommit\tmetric\tdelta\ttrigger_f1\ttokens\tguard\tstatus\tlayer\tdescription" >> <workspace>/evolve/results.tsv
```

---

## experiments.jsonl

per-case 细粒度实验记忆，每轮一条 JSON。

### 字段定义

| 字段 | 类型 | 说明 |
|---|---|---|
| iteration | int | 对应 results.tsv 的 iteration |
| mutation_type | string | 改动类型（body_rewrite / body_simplify / rule_reorder / template_change / script_fix 等） |
| mutation_layer | string | 改动层级（description / body / script） |
| intent | string | 改动意图（一句话） |
| changed_files | [string] | 被修改的文件列表 |
| cases_improved | [int] | 本轮变好的 case id 列表 |
| cases_degraded | [int] | 本轮变差的 case id 列表 |
| trigger_delta | float | trigger F1 变化量 |
| token_delta | int | tokens_mean 变化量 |
| status | string | keep / discard / crash / revert |
| failure_reason | string | 如果 discard/crash，简要原因 |

### 示例

```jsonl
{"iteration":1,"mutation_type":"body_rewrite","mutation_layer":"body","intent":"改进路径检索的易混淆提示","changed_files":["SKILL.md"],"cases_improved":[3,15],"cases_degraded":[],"trigger_delta":0.0,"token_delta":-20,"status":"keep","failure_reason":""}
{"iteration":2,"mutation_type":"body_simplify","mutation_layer":"body","intent":"简化 Pipeline 为两步","changed_files":["SKILL.md"],"cases_improved":[1],"cases_degraded":[3,23,40],"trigger_delta":-0.03,"token_delta":150,"status":"discard","failure_reason":"regression: 3 cases degraded, trigger dropped"}
```

---

## best_versions/

每次 keep 时，保存当前 skill 快照：

```bash
cp -r <skill-dir> <workspace>/evolve/best_versions/iteration-<N>/
```

保留最近 5 个 best version，更早的自动清理。

---

## Memory 读取协议

每轮 Phase 1 (Review) 必须读取：

1. `tail -20 <workspace>/evolve/results.tsv` → 看趋势和最近状态
2. `tail -10 <workspace>/evolve/experiments.jsonl` → 看细粒度失败原因
3. `git log --oneline -20` → 看改动历史
4. 统计 keeps/discards/crashes 比例 → 判断是否 stuck
