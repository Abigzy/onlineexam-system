"""题库模块的模型。"""

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

from apps.common.constants import Difficulty, QuestionType
from apps.common.models import TimeStampedModel

from .constants import CHOICE_TYPES, DEFAULT_SCORE_BY_TYPE, QuestionSource


class KnowledgePointQuerySet(models.QuerySet):
    """知识点查询集。"""

    def roots(self):
        return self.filter(parent__isnull=True)

    def active(self):
        return self.filter(is_active=True)

    def of_subject(self, subject: str):
        return self.filter(subject=subject) if subject else self

    def search(self, keyword: str):
        if not keyword:
            return self
        return self.filter(models.Q(name__icontains=keyword) | models.Q(code__icontains=keyword))

    def with_parent(self):
        return self.select_related("parent")

    def ordered(self):
        return self.order_by("subject", "order", "id")


class KnowledgePoint(TimeStampedModel):
    """知识点（树形结构，自关联）。

    用「自关联 + 同级 order」而不是第三方树形库：
      * 教学场景的知识点层级通常只有 2~3 层，够用；
      * 自关联查询简单、迁移干净、不引入额外依赖。
    """

    name = models.CharField("名称", max_length=64, db_index=True)
    code = models.CharField("编号", max_length=64, blank=True, default="", db_index=True,
                            help_text="如 CS-DS-01，便于导入导出时对齐")
    parent = models.ForeignKey(
        "self", verbose_name="上级知识点", null=True, blank=True,
        on_delete=models.CASCADE, related_name="children",
    )
    subject = models.CharField("所属学科/课程", max_length=64, blank=True, default="", db_index=True)
    description = models.TextField("描述", blank=True, default="")
    order = models.IntegerField("同级排序", default=0, help_text="数字越小越靠前")
    is_active = models.BooleanField("启用", default=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="创建人", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="created_knowledge_points",
    )

    objects = KnowledgePointQuerySet.as_manager()

    class Meta:
        db_table = "knowledge_point"
        verbose_name = "知识点"
        verbose_name_plural = "知识点"
        ordering = ["subject", "order", "id"]
        indexes = [
            models.Index(fields=["subject", "parent"], name="idx_kp_subject_parent"),
            models.Index(fields=["parent", "order"], name="idx_kp_parent_order"),
        ]
        constraints = [
            models.UniqueConstraint(fields=["subject", "name", "parent"], name="uniq_kp_subject_name_parent"),
        ]

    def __str__(self):
        return self.full_path

    # ------------------------------------------------------------------
    @property
    def full_path(self) -> str:
        """完整路径，如「数据结构 / 树 / 二叉树」。"""
        names, node, guard = [], self, 0
        while node is not None and guard < 10:
            names.append(node.name)
            node = node.parent
            guard += 1
        return " / ".join(reversed(names))

    @property
    def level(self) -> int:
        """层级，根节点为 1。"""
        depth, node, guard = 1, self.parent, 0
        while node is not None and guard < 10:
            depth += 1
            node = node.parent
            guard += 1
        return depth

    def descendant_ids(self, include_self: bool = True) -> list:
        """收集自身及所有后代的 id（用于「按知识点筛选含子知识点」）。"""
        if not self.pk:
            return []
        ids = [self.pk] if include_self else []
        frontier = [self.pk]
        guard = 0
        while frontier and guard < 10:
            children = list(
                KnowledgePoint.objects.filter(parent_id__in=frontier).values_list("id", flat=True)
            )
            if not children:
                break
            ids.extend(children)
            frontier = children
            guard += 1
        return ids

    def clean(self):
        # 防止把自己设成自己的上级（会形成环）
        if self.parent_id and self.pk and self.parent_id == self.pk:
            from django.core.exceptions import ValidationError

            raise ValidationError({"parent": "上级知识点不能是自己"})


class QuestionQuerySet(models.QuerySet):
    """题目查询集——把常用筛选收在这里，视图只写业务语义。"""

    def active(self):
        return self.filter(is_active=True)

    def of_type(self, question_type: str):
        return self.filter(type=question_type) if question_type else self

    def of_difficulty(self, difficulty):
        return self.filter(difficulty=difficulty) if difficulty else self

    def of_subject(self, subject: str):
        return self.filter(subject=subject) if subject else self

    def in_knowledge_points(self, knowledge_point_ids):
        """命中给定知识点（含其子知识点由调用方展开）的题目。"""
        if not knowledge_point_ids:
            return self
        return self.filter(knowledge_points__id__in=knowledge_point_ids).distinct()

    def search(self, keyword: str):
        if not keyword:
            return self
        return self.filter(
            models.Q(stem__icontains=keyword)
            | models.Q(reference_answer__icontains=keyword)
            | models.Q(analysis__icontains=keyword)
        )

    def with_relations(self):
        """列表页统一用这个，避免 N+1。"""
        return self.select_related("created_by").prefetch_related(
            "options", "knowledge_points"
        )

    def objective(self):
        return self.filter(type__in=QuestionType.objective_types())

    def subjective(self):
        return self.filter(type__in=QuestionType.subjective_types())

    def ai_generated(self):
        return self.filter(source=QuestionSource.AI)


class Question(TimeStampedModel):
    """题目。六种题型共用一张表，差异用 ``type`` 区分。

    答案存放约定（重要）：
      * 单选 / 多选 / 判断 —— 正确答案放在 ``QuestionOption.is_correct``；
        ``reference_answer`` 冗余存一份字母（如 "ABD"），方便导出与快速比对；
      * 填空 —— ``reference_answer`` 每行一个空的答案；
      * 简答 / 论述 —— ``reference_answer`` 是参考答案，按行拆成评分要点。
    """

    type = models.CharField("题型", max_length=16, choices=QuestionType.choices, db_index=True)
    stem = models.TextField("题干")
    difficulty = models.PositiveSmallIntegerField(
        "难度", choices=Difficulty.choices, default=Difficulty.MEDIUM, db_index=True
    )
    score = models.DecimalField(
        "默认分值", max_digits=6, decimal_places=2, default=2.00,
        validators=[MinValueValidator(0)],
    )
    reference_answer = models.TextField("参考答案", blank=True, default="")
    analysis = models.TextField("解析", blank=True, default="")

    knowledge_points = models.ManyToManyField(
        KnowledgePoint, verbose_name="知识点", blank=True, related_name="questions",
        db_table="question_knowledge_point",
    )

    subject = models.CharField("学科/课程", max_length=64, blank=True, default="", db_index=True)
    source = models.CharField("来源", max_length=16, choices=QuestionSource.choices,
                              default=QuestionSource.MANUAL, db_index=True)

    # AI 溯源：哪个平台、哪个模型生成的，方便复盘
    ai_provider = models.CharField("AI 平台", max_length=32, blank=True, default="")
    ai_model = models.CharField("AI 模型", max_length=64, blank=True, default="")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="创建人", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="created_questions",
    )
    is_active = models.BooleanField("启用", default=True, db_index=True)
    is_reviewed = models.BooleanField("已审核", default=True,
                                      help_text="AI 生成的题目建议教师审核后再使用")

    # 使用统计（被引用次数与正确次数，供后续学情分析用）
    usage_count = models.PositiveIntegerField("被作答次数", default=0)
    correct_count = models.PositiveIntegerField("答对次数", default=0)

    objects = QuestionQuerySet.as_manager()

    class Meta:
        db_table = "question"
        verbose_name = "题目"
        verbose_name_plural = "题目"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["type", "difficulty"], name="idx_q_type_difficulty"),
            models.Index(fields=["-created_at"], name="idx_q_created"),
            models.Index(fields=["is_active", "type"], name="idx_q_active_type"),
            models.Index(fields=["subject", "type"], name="idx_q_subject_type"),
        ]

    def __str__(self):
        return f"[{self.get_type_display()}] {self.stem[:40]}"

    # ------------------------------------------------------------------
    # 便捷属性
    # ------------------------------------------------------------------
    @property
    def is_choice(self) -> bool:
        return self.type in CHOICE_TYPES

    @property
    def is_subjective(self) -> bool:
        return self.type in QuestionType.subjective_types()

    @property
    def accuracy(self) -> float:
        """正确率，没有作答记录时返回 0。"""
        if not self.usage_count:
            return 0.0
        return round(self.correct_count / self.usage_count, 4)

    def correct_labels(self) -> list:
        """选择题的正确选项字母。"""
        return sorted(option.label for option in self.options.all() if option.is_correct)

    def correct_answer_text(self) -> str:
        """把参考答案标准化成字符串，判断题/选择题走选项。"""
        if self.is_choice:
            labels = self.correct_labels()
            return "".join(labels) if labels else (self.reference_answer or "")
        return self.reference_answer or ""

    def student_view(self) -> dict:
        """学生视角的题目数据——**绝不包含答案、解析、正确选项标记**。"""
        return {
            "id": self.id,
            "type": self.type,
            "type_display": self.get_type_display(),
            "stem": self.stem,
            "difficulty": self.difficulty,
            "score": float(self.score),
            "options": [
                {"label": option.label, "content": option.content}
                for option in sorted(self.options.all(), key=lambda o: (o.order, o.label))
            ],
        }


class QuestionOption(TimeStampedModel):
    """选择题选项。"""

    question = models.ForeignKey(
        Question, verbose_name="所属题目", on_delete=models.CASCADE, related_name="options"
    )
    label = models.CharField("选项标号", max_length=4, help_text="A / B / C / D ...")
    content = models.TextField("选项内容")
    is_correct = models.BooleanField("是否正确答案", default=False, db_index=True)
    order = models.IntegerField("排序", default=0)

    class Meta:
        db_table = "question_option"
        verbose_name = "题目选项"
        verbose_name_plural = "题目选项"
        ordering = ["question", "order", "label"]
        constraints = [
            models.UniqueConstraint(fields=["question", "label"], name="uniq_option_question_label"),
        ]

    def __str__(self):
        return f"{self.question_id}-{self.label}. {self.content[:20]}"


__all__ = [
    "DEFAULT_SCORE_BY_TYPE",
    "KnowledgePoint",
    "KnowledgePointQuerySet",
    "Question",
    "QuestionOption",
    "QuestionQuerySet",
]
