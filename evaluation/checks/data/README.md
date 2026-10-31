# 工程检查输入

本目录保存工程检查和历史知识探针的输入：

- `business-coverage-v1.json`：478 个显式业务查询输入，覆盖城市矩阵、费用计算和业务边界，由 `evaluation.checks.business_coverage` 执行。
- `knowledge_questions.yaml`：16 道历史业务知识问题。
- `policy_coverage.yaml`：40 道政策覆盖检查问题。
- `skill-knowledge.yaml`：30 道历史知识依赖问题，保留原名追溯来源，不产生独立 Skill 成绩。

这些输入不计入正式质量评测。正式题集见 [`evaluation/datasets/`](../../datasets/)，学习入口见[评测说明](../../README.md)，工程命令见[检查说明](../README.md)。旧固定台词和独立 Skill 案例已撤下，正式对话统一使用动态客户。
