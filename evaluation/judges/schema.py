"""整通对话质量评审的共享版本与维度，供裁判、指标和报告使用。"""

JUDGE_VERSION = "dialogue-rubric-v3.2.3"
QUALITY_DIMENSIONS = (
    "goal_completion",
    "problem_resolution",
    "factual_reliability",
    "communication_quality",
)
NOT_APPLICABLE_DIMENSIONS = {"problem_resolution", "factual_reliability"}

# 模型只输出易读的短名称，校验后映射为报告和指标沿用的内部维度名称。
JUDGE_RESPONSE_DIMENSIONS = {
    "goal": "goal_completion",
    "answer": "problem_resolution",
    "facts": "factual_reliability",
    "tone": "communication_quality",
}
