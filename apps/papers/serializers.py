"""试卷模块序列化器。

按视角分层，和题库模块保持一致的做法：
  * ``PaperListSerializer``            —— 列表，只给汇总信息；
  * ``PaperDetailSerializer``          —— 详情，含大题与全部题目（**含答案**）；
  * ``PaperQuestionStudentSerializer`` —— 学生视角，不含答案（模块四考试下发时用）。

组卷相关的入参都用普通 ``Serializer`` 手写校验：它们是「动作参数」而不是模型字段，
用 ModelSerializer 反而别扭。
"""

from decimal import Decimal

from rest_framework import serializers

from apps.common.constants import QuestionType
from apps.papers.constants import PaperStatus
from apps.papers.models import Paper, PaperQuestion, PaperSection
from apps.questions.models import Question


# ======================================================================
# 试卷结构
# ======================================================================
class PaperSectionSerializer(serializers.ModelSerializer):
    """试卷里的一个大题。"""

    question_count = serializers.IntegerField(read_only=True)
    subtotal = serializers.DecimalField(max_digits=8, decimal_places=2, read_only=True)
    label = serializers.SerializerMethodField()
    type_display = serializers.CharField(source="get_question_type_display", read_only=True)

    class Meta:
        model = PaperSection
        fields = ["id", "title", "question_type", "type_display", "description", "order",
                  "score_per_question", "question_count", "subtotal", "label"]

    def get_label(self, obj) -> str:
        return obj.section_label()


class PaperQuestionSerializer(serializers.ModelSerializer):
    """卷内的一道题（教师视角，含答案与解析）。"""

    question_id = serializers.IntegerField(source="question.id", read_only=True)
    stem = serializers.CharField(source="question.stem", read_only=True)
    reference_answer = serializers.CharField(source="question.reference_answer", read_only=True)
    analysis = serializers.CharField(source="question.analysis", read_only=True)
    type_display = serializers.CharField(source="get_question_type_display", read_only=True)
    difficulty_display = serializers.CharField(source="get_difficulty_display", read_only=True)
    section_title = serializers.SerializerMethodField()
    options = serializers.SerializerMethodField()
    knowledge_points = serializers.SerializerMethodField()
    usage_count = serializers.IntegerField(source="question.usage_count", read_only=True)
    accuracy = serializers.FloatField(source="question.accuracy", read_only=True)

    class Meta:
        model = PaperQuestion
        fields = ["id", "order", "score", "question_id", "section_id", "section_title",
                  "question_type", "type_display", "difficulty", "difficulty_display",
                  "stem", "options", "reference_answer", "analysis", "knowledge_points",
                  "usage_count", "accuracy"]

    def get_section_title(self, obj) -> str:
        return obj.section.title if obj.section_id else ""

    def get_options(self, obj) -> list:
        """带正误标记的选项——教师看清楚哪些是对的，学生端不走这个序列化器。"""
        return [
            {"label": option.label, "content": option.content, "is_correct": option.is_correct}
            for option in sorted(obj.question.options.all(), key=lambda o: (o.order, o.label))
        ]

    def get_knowledge_points(self, obj) -> list:
        return [{"id": kp.id, "name": kp.name} for kp in obj.question.knowledge_points.all()]


class PaperQuestionStudentSerializer(serializers.Serializer):
    """学生视角的卷内题目。

    这个序列化器不读模型字段，而是直接委托 ``PaperQuestion.student_payload()``——
    答案过滤的逻辑只有一处（``Question.student_view()``），不会因为多一条下发路径
    就多一个泄漏点。
    """

    def to_representation(self, instance: PaperQuestion) -> dict:
        return instance.student_payload()


# ======================================================================
# 试卷本体
# ======================================================================
class PaperListSerializer(serializers.ModelSerializer):
    """列表用：只给汇总，不带题目内容（列表页秒开的关键）。"""

    status_display = serializers.CharField(source="get_status_display", read_only=True)
    generation_mode_display = serializers.CharField(source="get_generation_mode_display", read_only=True)
    created_by_name = serializers.CharField(source="created_by.display_name", read_only=True, default="")
    total_score = serializers.FloatField(read_only=True)
    pass_score = serializers.FloatField(read_only=True)
    target_difficulty = serializers.FloatField(read_only=True)
    actual_difficulty = serializers.SerializerMethodField()
    section_count = serializers.SerializerMethodField()

    class Meta:
        model = Paper
        fields = ["id", "title", "subject", "status", "status_display",
                  "generation_mode", "generation_mode_display", "duration_minutes",
                  "total_score", "pass_score", "question_count", "target_difficulty",
                  "actual_difficulty", "section_count", "created_by_name",
                  "published_at", "created_at", "updated_at"]

    def get_actual_difficulty(self, obj) -> float:
        return obj.actual_difficulty()

    def get_section_count(self, obj) -> int:
        return len(obj.sections.all())


class PaperDetailSerializer(PaperListSerializer):
    """详情：卷头 + 大题 + 全部题目 + 组卷说明。"""

    sections = serializers.SerializerMethodField()
    paper_questions = serializers.SerializerMethodField()

    class Meta(PaperListSerializer.Meta):
        fields = PaperListSerializer.Meta.fields + [
            "description", "sections", "paper_questions",
            "ai_notes", "ai_provider", "ai_model",
        ]

    def get_sections(self, obj) -> list:
        sections = sorted(obj.sections.all(), key=lambda s: (s.order, s.id))
        return PaperSectionSerializer(sections, many=True).data

    def get_paper_questions(self, obj) -> list:
        items = sorted(obj.paper_questions.all(), key=lambda item: (item.order, item.id))
        return PaperQuestionSerializer(items, many=True).data


class PaperWriteSerializer(serializers.ModelSerializer):
    """新建 / 修改卷头。

    ``question_ids`` 只在新建时有意义（建完顺手塞题）；改卷头时忽略它，
    加题走专门的接口，避免一次请求里混着两件事。
    """

    question_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, allow_empty=True, write_only=True,
    )

    class Meta:
        model = Paper
        fields = ["title", "subject", "description", "duration_minutes",
                  "pass_score", "target_difficulty", "question_ids"]

    def validate_title(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("试卷标题不能为空")
        return value

    def validate_duration_minutes(self, value):
        if value is None or value <= 0:
            raise serializers.ValidationError("考试时长必须大于 0")
        if value > 600:
            raise serializers.ValidationError("考试时长不应超过 600 分钟")
        return value

    def validate_pass_score(self, value):
        if value is not None and value < 0:
            raise serializers.ValidationError("及格分不能为负数")
        return value

    def validate_target_difficulty(self, value):
        if value is None:
            return 2.0
        if not 1 <= float(value) <= 4:
            raise serializers.ValidationError("目标难度系数应在 1（简单）到 4（挑战）之间")
        return float(value)


# ======================================================================
# 动作入参
# ======================================================================
class AddQuestionsSerializer(serializers.Serializer):
    """往卷子里加题。"""

    question_ids = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=False, min_length=1,
    )
    score = serializers.DecimalField(max_digits=6, decimal_places=2, required=False, min_value=Decimal("0.5"))
    strict = serializers.BooleanField(
        default=True,
        help_text="True=遇到已在卷内的题直接报错；False=静默跳过（AI 组卷用）",
    )

    def validate_question_ids(self, value):
        existing = set(Question.objects.filter(id__in=value).values_list("id", flat=True))
        missing = [qid for qid in value if qid not in existing]
        if missing:
            raise serializers.ValidationError(f"以下题目不存在：{missing}")
        return value


class BatchScoreSerializer(serializers.Serializer):
    """批量调分。"""

    class ItemSerializer(serializers.Serializer):
        id = serializers.IntegerField()
        score = serializers.DecimalField(max_digits=6, decimal_places=2, min_value=Decimal("0.5"))

    items = ItemSerializer(many=True, allow_empty=False)


class RemoveQuestionsSerializer(serializers.Serializer):
    """把题目移出试卷。

    传的是 ``paper_question.id``（卷内条目 id）而**不是** ``question.id``：
    同一道题理论上可以出现在不同卷子里，按卷内条目定位才不会误删别的卷子。
    """

    ids = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=False, min_length=1,
        help_text="卷内题目条目 id（PaperQuestion.id）",
    )


class ReorderSerializer(serializers.Serializer):
    """调整题目顺序。"""

    ordered_ids = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=False, min_length=1,
    )


class StatusChangeSerializer(serializers.Serializer):
    """状态流转。"""

    status = serializers.ChoiceField(choices=PaperStatus.choices)


class CloneSerializer(serializers.Serializer):
    """复制试卷。"""

    title = serializers.CharField(required=False, allow_blank=True, max_length=200)


# ======================================================================
# 智能组卷
# ======================================================================
class BlueprintItemSerializer(serializers.Serializer):
    """组卷蓝图的一行：某题型几道题、每题几分。"""

    type = serializers.ChoiceField(choices=QuestionType.choices)
    count = serializers.IntegerField(min_value=1, max_value=100)
    score = serializers.DecimalField(max_digits=6, decimal_places=2, min_value=Decimal("0.5"))


class ComposeRequestSerializer(serializers.Serializer):
    """智能组卷入参。

    ``dry_run=True`` 时只试算不落库，用来先给教师看「能不能组出来、大概什么难度」。
    """

    title = serializers.CharField(required=False, allow_blank=True, max_length=200)
    subject = serializers.CharField(required=False, allow_blank=True, max_length=64)
    knowledge_point_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, allow_empty=True,
        help_text="要覆盖的知识点（自动包含其子知识点）；不传表示全题库范围",
    )
    blueprint = BlueprintItemSerializer(many=True, required=False, allow_empty=False)
    target_difficulty = serializers.FloatField(default=2.0, min_value=1.0, max_value=4.0)
    duration_minutes = serializers.IntegerField(default=90, min_value=1, max_value=600)
    pass_score = serializers.DecimalField(
        max_digits=7, decimal_places=2, required=False, min_value=0,
    )
    exclude_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, allow_empty=True,
        help_text="要排除的题目 id（如刚考过的题）",
    )
    seed = serializers.IntegerField(
        required=False, allow_null=True, default=None,
        help_text="随机种子；传相同值可复现同一份卷子",
    )
    dry_run = serializers.BooleanField(default=False)
    generate_notes = serializers.BooleanField(
        default=True, help_text="是否调用 AI 生成组卷说明",
    )

    def validate_blueprint(self, value):
        if not value:
            return value
        # 同一题型在蓝图里出现两次，另一行的配额就会被悄悄忽略——直接拦掉
        types = [row["type"] for row in value]
        duplicated = {t for t in types if types.count(t) > 1}
        if duplicated:
            labels = [dict(QuestionType.choices)[t] for t in duplicated]
            raise serializers.ValidationError(f"蓝图里同一题型只能出现一次：{'、'.join(labels)}")
        return value


class RandomComposeRequestSerializer(serializers.Serializer):
    """随机组卷入参：只选知识点 + 题量，蓝图由系统按题库库存自动生成，不调 AI。"""

    title = serializers.CharField(required=False, allow_blank=True, max_length=200)
    subject = serializers.CharField(required=False, allow_blank=True, max_length=64)
    knowledge_point_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, allow_empty=True,
        help_text="要覆盖的知识点（自动包含其子知识点）；不传表示全题库范围",
    )
    total_count = serializers.IntegerField(
        default=20, min_value=1, max_value=200,
        help_text="期望题量；蓝图会按题库库存自动分配题型，实际可能因库存不足而少于该值",
    )
    target_difficulty = serializers.FloatField(default=2.0, min_value=1.0, max_value=4.0)
    duration_minutes = serializers.IntegerField(default=90, min_value=1, max_value=600)
    pass_score = serializers.DecimalField(
        max_digits=7, decimal_places=2, required=False, min_value=0,
    )
    exclude_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, allow_empty=True,
        help_text="要排除的题目 id（如刚考过的题）",
    )
    seed = serializers.IntegerField(
        required=False, allow_null=True, default=None,
        help_text="随机种子；传相同值可复现同一份卷子",
    )
    dry_run = serializers.BooleanField(default=False)


class PoolQuerySerializer(serializers.Serializer):
    """查询题库家底（组卷工作台左侧的「可用题量」）。"""

    subject = serializers.CharField(required=False, allow_blank=True, max_length=64)
    knowledge_point_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, allow_empty=True,
    )


class ExplainPaperSerializer(serializers.Serializer):
    """生成 / 重新生成组卷说明。"""

    provider = serializers.CharField(required=False, allow_blank=True, max_length=32)
    force = serializers.BooleanField(default=False, help_text="已有说明时是否覆盖重写")


__all__ = [
    "AddQuestionsSerializer",
    "BatchScoreSerializer",
    "BlueprintItemSerializer",
    "CloneSerializer",
    "ComposeRequestSerializer",
    "ExplainPaperSerializer",
    "PaperDetailSerializer",
    "PaperListSerializer",
    "PaperQuestionSerializer",
    "PaperQuestionStudentSerializer",
    "PaperSectionSerializer",
    "PaperWriteSerializer",
    "PoolQuerySerializer",
    "RemoveQuestionsSerializer",
    "ReorderSerializer",
    "StatusChangeSerializer",
]
