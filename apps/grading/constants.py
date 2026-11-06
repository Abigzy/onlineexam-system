"""判分与成绩模块常量。

这里集中定义三类东西：

1. **多选题给分策略** —— 枚举本体定义在 ``apps.exam.constants``（因为它是 ``Exam``
   上的一列），这里只做转出，保证「考试给分策略」在整个系统里只有一份定义。
2. **主观题阈值** —— 与需求文档一致：相似度 ≥0.7 满分、0.4~0.7 部分得分、
   <0.4 不得分。阈值写在常量里，`apps.ai.schemas.score_by_threshold()` 的调用也
   引用这里，避免两处各写一份数字。
3. **复核口径** —— 教师对 AI 结果的三种处置方式，用于「AI 批改准确率」的校准统计。
"""

from django.db import models

# 多选题给分策略：定义在考试模块（它是 Exam 的字段），此处转出复用
from apps.exam.constants import (  # noqa: F401  （对外转出）
    DEFAULT_MULTIPLE_CHOICE_POLICY,
    MultipleChoicePolicy,
)


# ==================================================================
# 复核与校准
# ==================================================================
class GradeSource(models.TextChoices):
    """教师复核时，「原来的分数」是怎么来的。"""

    AUTO = "auto", "客观题自动判分"
    AI = "ai", "AI 批改建议"
    NONE = "none", "原无评分，教师直接给分"


class ReviewAction(models.TextChoices):
    """教师对已有评分结果的处置方式（校准统计按这个口径算采纳率）。"""

    ACCEPT = "accept", "直接采纳建议分"
    ADJUST = "adjust", "在建议分基础上调整"
    OVERRIDE = "override", "完全自评"


# ==================================================================
# 主观题阈值（与需求文档 / apps.ai.schemas 保持一致）
# ==================================================================
# 得分比例 ≥ 该值 → 满分
SUBJECTIVE_FULL_THRESHOLD = 0.7
# 得分比例 ≥ 该值（且未达满分线）→ 线性插值给部分分
SUBJECTIVE_PARTIAL_THRESHOLD = 0.4

# 错题本判定：主观题得分率低于该值也算「错题」
WRONG_BOOK_SUBJECTIVE_RATIO = 0.6

# 单次请求里最多批改多少道主观题——防止一个请求把大模型打到超时
AI_GRADE_MAX_QUESTIONS = 30


__all__ = [
    "AI_GRADE_MAX_QUESTIONS",
    "DEFAULT_MULTIPLE_CHOICE_POLICY",
    "GradeSource",
    "MultipleChoicePolicy",
    "ReviewAction",
    "SUBJECTIVE_FULL_THRESHOLD",
    "SUBJECTIVE_PARTIAL_THRESHOLD",
    "WRONG_BOOK_SUBJECTIVE_RATIO",
]
