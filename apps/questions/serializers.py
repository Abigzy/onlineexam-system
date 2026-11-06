"""
题库模块序列化器。

**最重要的一条规则**：答案相关的字段（``reference_answer`` / ``analysis`` /
``options[].is_correct``）绝不能出现在学生能拿到的序列化器里。
因此这里把「教师视角」和「学生视角」拆成两套序列化器，而不是靠前端隐藏。
"""

from rest_framework import serializers

from apps.common.constants import Difficulty, QuestionType

from .constants import DEFAULT_SCORE_BY_TYPE, QuestionSource
from .models import KnowledgePoint, Question, QuestionOption


# ==================================================================
# 知识点
# ==================================================================
class KnowledgePointSerializer(serializers.ModelSerializer):
    """知识点基础信息。"""

    full_path = serializers.CharField(read_only=True)
    level = serializers.IntegerField(read_only=True)
    parent_name = serializers.CharField(source="parent.name", read_only=True, default="")
    question_count = serializers.SerializerMethodField()
    children_count = serializers.SerializerMethodField()

    class Meta:
        model = KnowledgePoint
        fields = ["id", "name", "code", "parent", "parent_name", "subject", "description",
                  "order", "is_active", "full_path", "level",
                  "question_count", "children_count", "created_at"]
        read_only_fields = ["id", "created_at", "full_path", "level", "question_count", "children_count"]

    def get_question_count(self, obj) -> int:
        # 列表接口用 annotate 预计算，没有时兜底查一次
        annotated = getattr(obj, "question_total", None)
        return annotated if annotated is not None else obj.questions.count()

    def get_children_count(self, obj) -> int:
        annotated = getattr(obj, "children_total", None)
        return annotated if annotated is not None else obj.children.count()

    def validate_parent(self, value):
        if value is None:
            return value
        if self.instance and (value.pk == self.instance.pk):
            raise serializers.ValidationError("上级知识点不能是自己")
        if self.instance and value.pk in set(self.instance.descendant_ids()):
            raise serializers.ValidationError("上级知识点不能是自己的子节点（会形成环）")
        return value

    def validate(self, attrs):
        subject = attrs.get("subject", getattr(self.instance, "subject", "") or "")
        name = attrs.get("name", getattr(self.instance, "name", "") or "")
        parent = attrs.get("parent", getattr(self.instance, "parent", None))

        duplicated = KnowledgePoint.objects.filter(subject=subject, name=name, parent=parent)
        if self.instance:
            duplicated = duplicated.exclude(pk=self.instance.pk)
        if duplicated.exists():
            raise serializers.ValidationError({"name": "同级下已存在同名知识点"})
        return attrs


class KnowledgePointTreeSerializer(KnowledgePointSerializer):
    """树形知识点（后端递归组装，前端直接渲染即可）。"""

    children = serializers.SerializerMethodField()

    class Meta(KnowledgePointSerializer.Meta):
        fields = KnowledgePointSerializer.Meta.fields + ["children"]

    def get_children(self, obj) -> list:
        # 用 view 预先构建好的 children_map，避免在序列化里反复查库
        children_map = (self.context or {}).get("children_map") or {}
        children = children_map.get(obj.pk, [])
        return KnowledgePointTreeSerializer(children, many=True, context=self.context).data


# ==================================================================
# 题目
# ==================================================================
class QuestionOptionSerializer(serializers.ModelSerializer):
    """选项（教师视角，含正确答案标记）。"""

    class Meta:
        model = QuestionOption
        fields = ["id", "label", "content", "is_correct", "order"]


class QuestionOptionInputSerializer(serializers.Serializer):
    """新建/更新题目时传入的选项。"""

    label = serializers.CharField(max_length=4, required=False, allow_blank=True)
    content = serializers.CharField()
    is_correct = serializers.BooleanField(required=False, default=False)
    order = serializers.IntegerField(required=False)


class QuestionListSerializer(serializers.ModelSerializer):
    """列表用的轻量序列化器（不含选项明细，避免列表接口过大）。"""

    type_display = serializers.CharField(source="get_type_display", read_only=True)
    difficulty_display = serializers.CharField(source="get_difficulty_display", read_only=True)
    source_display = serializers.CharField(source="get_source_display", read_only=True)
    knowledge_points = serializers.SerializerMethodField()
    accuracy = serializers.FloatField(read_only=True)
    option_count = serializers.SerializerMethodField()
    created_by_name = serializers.CharField(source="created_by.display_name", read_only=True, default="")

    class Meta:
        model = Question
        fields = ["id", "type", "type_display", "stem", "difficulty", "difficulty_display",
                  "score", "subject", "source", "source_display", "is_active", "is_reviewed",
                  "knowledge_points", "option_count", "usage_count", "correct_count", "accuracy",
                  "created_by_name", "ai_provider", "ai_model", "created_at", "updated_at"]

    def get_knowledge_points(self, obj) -> list:
        return [{"id": kp.id, "name": kp.name} for kp in obj.knowledge_points.all()]

    def get_option_count(self, obj) -> int:
        return len(obj.options.all())


class QuestionDetailSerializer(QuestionListSerializer):
    """详情（教师视角，含答案与解析）。"""

    options = QuestionOptionSerializer(many=True, read_only=True)

    class Meta(QuestionListSerializer.Meta):
        fields = QuestionListSerializer.Meta.fields + ["options", "reference_answer", "analysis"]


class QuestionStudentSerializer(serializers.ModelSerializer):
    """学生视角——**不含答案、解析、正确选项标记**。

    考试模块下发试卷时用这个，从根上杜绝答案泄漏。
    """

    type_display = serializers.CharField(source="get_type_display", read_only=True)
    options = serializers.SerializerMethodField()

    class Meta:
        model = Question
        fields = ["id", "type", "type_display", "stem", "difficulty", "score", "options"]

    def get_options(self, obj) -> list:
        return [
            {"label": option.label, "content": option.content}
            for option in sorted(obj.options.all(), key=lambda o: (o.order, o.label))
        ]


class QuestionWriteSerializer(serializers.ModelSerializer):
    """新建 / 更新题目。

    选项与知识点的落库逻辑交给 ``QuestionService``，这里只负责字段级校验。
    """

    options = QuestionOptionInputSerializer(many=True, required=False, write_only=True)
    knowledge_point_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, write_only=True,
        help_text="已存在的知识点 id 列表",
    )
    knowledge_point_names = serializers.ListField(
        child=serializers.CharField(), required=False, write_only=True,
        help_text="知识点名称列表，不存在的会自动创建为该学科下的根知识点",
    )

    class Meta:
        model = Question
        fields = ["type", "stem", "difficulty", "score", "reference_answer", "analysis",
                  "subject", "knowledge_point_ids", "knowledge_point_names", "options",
                  "is_active", "is_reviewed", "source"]

    def validate_stem(self, value):
        value = (value or "").strip()
        if len(value) < 2:
            raise serializers.ValidationError("题干内容过短")
        return value

    def validate_score(self, value):
        if value is None:
            return value
        if value <= 0:
            raise serializers.ValidationError("分值必须大于 0")
        if value > 100:
            raise serializers.ValidationError("单题分值不应超过 100")
        return value

    def validate_type(self, value):
        if value not in dict(QuestionType.choices):
            raise serializers.ValidationError("不支持的题型")
        return value

    def validate(self, attrs):
        """跨字段校验：题型与选项、答案的匹配关系。"""
        question_type = attrs.get("type", getattr(self.instance, "type", None))
        options = attrs.get("options")
        reference_answer = attrs.get("reference_answer", getattr(self.instance, "reference_answer", ""))

        if question_type in (QuestionType.SINGLE, QuestionType.MULTIPLE, QuestionType.JUDGE):
            if options is None and self.instance is None:
                raise serializers.ValidationError({"options": "选择题必须提供选项"})
        else:
            if not (reference_answer or "").strip() and self.instance is None:
                raise serializers.ValidationError(
                    {"reference_answer": f"{QuestionType(question_type).label}必须填写参考答案"}
                )
        return attrs

    def to_internal_value(self, data):
        """补默认分值：教师没填分值时按题型给一个合理值。"""
        attrs = super().to_internal_value(data)
        if "score" not in attrs:
            question_type = attrs.get("type") or getattr(self.instance, "type", QuestionType.SINGLE)
            attrs["score"] = DEFAULT_SCORE_BY_TYPE.get(question_type, 2.0)
        return attrs


class QuestionDraftSerializer(serializers.Serializer):
    """AI 生成草稿的入参（教师确认后批量入库）。"""

    type = serializers.ChoiceField(choices=QuestionType.choices)
    stem = serializers.CharField()
    difficulty = serializers.ChoiceField(choices=Difficulty.choices, required=False,
                                         default=Difficulty.MEDIUM)
    score = serializers.FloatField(required=False, allow_null=True)
    options = QuestionOptionInputSerializer(many=True, required=False)
    reference_answer = serializers.CharField(required=False, allow_blank=True, default="")
    analysis = serializers.CharField(required=False, allow_blank=True, default="")
    knowledge_point_names = serializers.ListField(child=serializers.CharField(), required=False)
    knowledge_point_ids = serializers.ListField(child=serializers.IntegerField(), required=False)


class QuestionBatchCreateSerializer(serializers.Serializer):
    """批量入库入参。"""

    items = QuestionDraftSerializer(many=True)
    subject = serializers.CharField(required=False, allow_blank=True, default="")
    source = serializers.ChoiceField(choices=QuestionSource.choices, required=False,
                                     default=QuestionSource.AI)
    is_reviewed = serializers.BooleanField(required=False, default=True)


# ==================================================================
# AI 出题
# ==================================================================
class AIGenerateRequestSerializer(serializers.Serializer):
    """AI 出题入参。"""

    subject = serializers.CharField(required=False, allow_blank=True, default="")
    knowledge_points = serializers.ListField(
        child=serializers.CharField(), required=False, default=list,
        help_text="知识点名称列表",
    )
    knowledge_point_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False,
        help_text="也可以直接传知识点 id，会自动补出名称",
    )
    question_types = serializers.ListField(
        child=serializers.ChoiceField(choices=QuestionType.choices),
        required=False, default=[QuestionType.SINGLE],
    )
    difficulty = serializers.ChoiceField(choices=Difficulty.choices, required=False,
                                         default=Difficulty.MEDIUM)
    count = serializers.IntegerField(required=False, default=5, min_value=1, max_value=20)
    source_text = serializers.CharField(required=False, allow_blank=True, default="",
                                        help_text="教材段落 / 命题素材")
    provider = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        ids = attrs.get("knowledge_point_ids") or []
        if ids and not attrs.get("knowledge_points"):
            names = list(
                KnowledgePoint.objects.filter(id__in=ids).values_list("name", flat=True)
            )
            attrs["knowledge_points"] = names
        if not attrs.get("source_text") and not attrs.get("knowledge_points"):
            raise serializers.ValidationError(
                {"source_text": "请至少提供命题素材或知识点，否则 AI 无从下手"}
            )
        return attrs
