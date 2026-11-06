"""判分与成绩模块序列化器。

只做两件事：**校验入参** 与 **输出复核记录**。
成绩单 / 错题本 / 班级成绩单这些「读模型」由 ``services.ScoreReportService``
直接返回字典——它们的结构跟数据库表不是一一对应，硬套 ModelSerializer
只会写出一堆 ``SerializerMethodField``，反而更难读。
"""

from rest_framework import serializers

from apps.exam.constants import MultipleChoicePolicy

from .constants import ReviewAction
from .models import GradingFeedback


# ======================================================================
# 入参
# ======================================================================
class AttemptGradeSerializer(serializers.Serializer):
    """判分入参。"""

    with_ai = serializers.BooleanField(
        required=False, default=False,
        help_text="是否同时调用大模型批改主观题（会消耗 token，默认关闭）",
    )
    policy = serializers.ChoiceField(
        choices=MultipleChoicePolicy.choices, required=False, allow_null=True, allow_blank=True,
        help_text="多选题给分策略；不传则用考试自己的配置",
    )
    provider = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, max_length=32,
        help_text="指定 AI 平台（deepseek / doubao / openai / mock）；不传用项目默认",
    )
    force = serializers.BooleanField(
        required=False, default=False,
        help_text="已出分的作答是否允许重新判分（默认拒绝，避免覆盖人工复核结果）",
    )


class AnswerReviewSerializer(serializers.Serializer):
    """教师复核一道题。

    两种用法互斥但都支持：
      * ``accept_ai=true`` —— 一键采纳 AI 建议分；
      * 给 ``score``      —— 教师自己定分。
    """

    score = serializers.DecimalField(
        max_digits=6, decimal_places=2, required=False, allow_null=True,
        help_text="复核后的分数；accept_ai=true 时忽略",
    )
    comment = serializers.CharField(required=False, allow_blank=True, default="",
                                    help_text="教师评语，会覆盖原评语")
    accept_ai = serializers.BooleanField(required=False, default=False)
    action = serializers.ChoiceField(choices=ReviewAction.choices, required=False,
                                     allow_null=True, allow_blank=True,
                                     help_text="处置方式；不传则按有无 AI 建议自动判定")

    def validate(self, attrs):
        if not attrs.get("accept_ai") and attrs.get("score") is None:
            raise serializers.ValidationError(
                {"score": "请填写复核分数，或使用「采纳 AI 建议」(accept_ai=true)"}
            )
        return attrs


class AttemptFinalizeSerializer(serializers.Serializer):
    """出分（目前无需参数，留出结构以便将来加「通知学生」等开关）。"""

    notify = serializers.BooleanField(required=False, default=False)


# ======================================================================
# 出参
# ======================================================================
class GradingFeedbackSerializer(serializers.ModelSerializer):
    """复核记录。"""

    reviewer_name = serializers.CharField(source="reviewer.display_name", read_only=True,
                                          default="")
    action_display = serializers.CharField(source="get_action_display", read_only=True)
    source_display = serializers.CharField(source="get_source_display", read_only=True)
    ai_delta = serializers.SerializerMethodField()
    exam_title = serializers.CharField(source="attempt.exam.title", read_only=True, default="")
    student_name = serializers.CharField(source="attempt.student.display_name",
                                         read_only=True, default="")
    stem = serializers.SerializerMethodField()

    class Meta:
        model = GradingFeedback
        fields = ["id", "answer_id", "attempt_id", "exam_title", "student_name",
                  "reviewer_id", "reviewer_name", "question_type",
                  "source", "source_display", "action", "action_display",
                  "ai_score", "ai_ratio", "original_score", "final_score",
                  "delta", "ai_delta", "full_score", "comment", "stem", "created_at"]

    def get_ai_delta(self, obj):
        value = obj.ai_delta
        return float(value) if value is not None else None

    def get_stem(self, obj) -> str:
        return obj.answer.paper_question.question.stem


__all__ = [
    "AnswerReviewSerializer",
    "AttemptFinalizeSerializer",
    "AttemptGradeSerializer",
    "GradingFeedbackSerializer",
]
