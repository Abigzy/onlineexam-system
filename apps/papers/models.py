"""试卷与组卷模块的模型。

三层结构：``Paper``（试卷头）→ ``PaperSection``（大题分区）→ ``PaperQuestion``（题目）。

为什么要分「大题分区」这一层？
  真实卷子从来不是一坨题按顺序排的，而是「一、单项选择（10×2分）二、填空（5×3分）…」。
  分区把「题型 + 每题分值 + 大题说明」聚在一起，考试端可以直接按分区渲染出卷面。
"""

from decimal import Decimal

from django.conf import settings
from django.db import models

from apps.common.constants import Difficulty, QuestionType
from apps.common.models import TimeStampedModel

from .constants import PaperGenerationMode, PaperStatus


class PaperQuerySet(models.QuerySet):
    """试卷查询集。"""

    def active(self):
        """未归档（默认列表用）。"""
        return self.exclude(status=PaperStatus.ARCHIVED)

    def of_status(self, status: str):
        return self.filter(status=status) if status else self

    def of_subject(self, subject: str):
        return self.filter(subject=subject) if subject else self

    def published(self):
        return self.filter(status=PaperStatus.PUBLISHED)

    def search(self, keyword: str):
        if not keyword:
            return self
        return self.filter(
            models.Q(title__icontains=keyword)
            | models.Q(description__icontains=keyword)
            | models.Q(subject__icontains=keyword)
        )

    def with_relations(self):
        """列表页统一用这个，避免 N+1。"""
        return self.select_related("created_by").prefetch_related(
            "sections", "paper_questions__question__options", "paper_questions__question__knowledge_points"
        )

    def with_counts(self):
        """列表页只需要数量，不必把题目全拉出来。"""
        return self.annotate(
            section_total=models.Count("sections", distinct=True),
            item_total=models.Count("paper_questions", distinct=True),
        )


class Paper(TimeStampedModel):
    """试卷（卷头 + 汇总信息）。

    ``total_score`` / ``question_count`` 是**冗余汇总字段**，由 ``PaperService.recalc()``
    统一维护。冗余的理由：列表页与考试列表都要显示总分和题量，每次都去聚合
    ``paper_question`` 太浪费；而这个值只在增删题/调分时变，维护成本很低。
    """

    title = models.CharField("试卷标题", max_length=200, db_index=True)
    subject = models.CharField("学科/课程", max_length=64, blank=True, default="", db_index=True)
    description = models.TextField("说明", blank=True, default="",
                                   help_text="试卷说明，会印在卷首")

    status = models.CharField("状态", max_length=16, choices=PaperStatus.choices,
                              default=PaperStatus.DRAFT, db_index=True)
    generation_mode = models.CharField("组卷方式", max_length=16, choices=PaperGenerationMode.choices,
                                       default=PaperGenerationMode.MANUAL, db_index=True)

    duration_minutes = models.PositiveIntegerField("考试时长（分钟）", default=90)
    total_score = models.DecimalField("总分", max_digits=7, decimal_places=2, default=Decimal("0.00"))
    pass_score = models.DecimalField("及格分", max_digits=7, decimal_places=2, default=Decimal("60.00"))
    question_count = models.PositiveIntegerField("题目数量", default=0)

    target_difficulty = models.FloatField(
        "目标难度系数", default=2.0, help_text="1=简单 2=中等 3=困难 4=挑战；组卷后用来比对实际难度",
    )

    # AI 组卷的「说明 / 选题理由」，由大模型生成，纯文本展示
    ai_notes = models.TextField("组卷说明（AI）", blank=True, default="")
    ai_provider = models.CharField("AI 平台", max_length=32, blank=True, default="")
    ai_model = models.CharField("AI 模型", max_length=64, blank=True, default="")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="创建人", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="created_papers",
    )
    published_at = models.DateTimeField("发布时间", null=True, blank=True)

    objects = PaperQuerySet.as_manager()

    class Meta:
        db_table = "paper"
        verbose_name = "试卷"
        verbose_name_plural = "试卷"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"], name="idx_paper_status_created"),
            models.Index(fields=["subject", "status"], name="idx_paper_subject_status"),
        ]

    def __str__(self):
        return self.title

    # ------------------------------------------------------------------
    # 便捷属性
    # ------------------------------------------------------------------
    @property
    def is_editable(self) -> bool:
        """草稿才能自由改动；已发布/归档的卷子要改，先退回草稿。"""
        return self.status == PaperStatus.DRAFT

    @property
    def is_published(self) -> bool:
        return self.status == PaperStatus.PUBLISHED

    @property
    def total_score_float(self) -> float:
        return float(self.total_score or 0)

    def actual_difficulty(self) -> float:
        """实际平均难度系数（按每题分值加权）。

        加权而不是简单平均：一道 10 分的难题对整卷难度的影响本来就比 1 分的判断题大。
        没有题目时返回 0。
        """
        rows = list(self.paper_questions.values_list("difficulty", "score"))
        if not rows:
            return 0.0
        weight_sum = sum(float(score) for _, score in rows)
        if weight_sum <= 0:
            return round(sum(int(d) for d, _ in rows) / len(rows), 2)
        weighted = sum(int(difficulty) * float(score) for difficulty, score in rows)
        return round(weighted / weight_sum, 2)

    def difficulty_deviation(self) -> float:
        """实际难度与目标难度的偏差（正数表示偏难）。"""
        actual = self.actual_difficulty()
        if not actual:
            return 0.0
        return round(actual - float(self.target_difficulty or 0), 2)

    def knowledge_point_ids(self) -> set:
        """本卷涉及的全部知识点 id（去重）。"""
        return set(
            self.paper_questions.values_list("question__knowledge_points__id", flat=True).distinct()
        ) - {None}

    def type_distribution(self) -> dict:
        """题型分布：{题型: 题量}。"""
        rows = self.paper_questions.values("question_type").annotate(count=models.Count("id"))
        return {row["question_type"]: row["count"] for row in rows}


class PaperSection(TimeStampedModel):
    """试卷里的一道大题（分区）。

    分区的存在让「一、单项选择 10×2 分」这种结构能原样落到数据里，
    考试页可以按分区渲染大题说明，学情分析也能按大题看得分率。
    """

    paper = models.ForeignKey(Paper, verbose_name="所属试卷", on_delete=models.CASCADE,
                              related_name="sections")
    title = models.CharField("大题标题", max_length=100, help_text="如「一、单项选择题」")
    question_type = models.CharField("题型", max_length=16, choices=QuestionType.choices)
    description = models.TextField("大题说明", blank=True, default="",
                                   help_text="如「每题只有一个正确答案」")
    order = models.IntegerField("排序", default=0, help_text="数字越小越靠前")
    score_per_question = models.DecimalField(
        "每题分值", max_digits=6, decimal_places=2, null=True, blank=True,
        help_text="留空表示各题分值不一（在题目上单独设置）",
    )

    class Meta:
        db_table = "paper_section"
        verbose_name = "试卷大题"
        verbose_name_plural = "试卷大题"
        ordering = ["paper", "order", "id"]
        indexes = [
            models.Index(fields=["paper", "order"], name="idx_psection_paper_order"),
        ]

    def __str__(self):
        return f"{self.paper_id}-{self.title}"

    # ------------------------------------------------------------------
    @property
    def question_count(self) -> int:
        return self.paper_questions.count()

    @property
    def subtotal(self) -> Decimal:
        """本大题小计分数。"""
        total = self.paper_questions.aggregate(models.Sum("score"))["score__sum"]
        return total or Decimal("0.00")

    def section_label(self) -> str:
        """卷面上显示的一行，如「一、单项选择题（共 10 题，每题 2 分，共 20 分）」。"""
        count = self.question_count
        subtotal = self.subtotal
        if count == 0:
            return self.title
        if self.score_per_question:
            per = self._fmt(self.score_per_question)
            return f"{self.title}（共 {count} 题，每题 {per} 分，共 {self._fmt(subtotal)} 分）"
        return f"{self.title}（共 {count} 题，共 {self._fmt(subtotal)} 分）"

    @staticmethod
    def _fmt(value) -> str:
        """去掉多余的小数点，12.00 → 12，12.50 → 12.5。"""
        number = float(value)
        return str(int(number)) if number == int(number) else f"{number:g}"


class PaperQuestion(TimeStampedModel):
    """试卷中的一道题。

    三个刻意的设计：
      1. ``score`` 是**本卷的分值快照**，不是 ``Question.score``。教师给这份卷子
         单独调分，不应该影响题库里的默认分值。
      2. ``question_type`` / ``difficulty`` 也做快照。理由：组卷质量分析要对
         几十份卷子做聚合，直接读本表比 JOIN 题库快；更要紧的是**已发布卷子的
         难度画像不应该因为后来有人改了题库里那道题的难度而变形**。
      3. ``question`` 用 ``PROTECT``：题目被引用在试卷里就不允许物理删除，
         避免悄悄把历史卷子掏空。
    """

    paper = models.ForeignKey(Paper, verbose_name="所属试卷", on_delete=models.CASCADE,
                              related_name="paper_questions")
    section = models.ForeignKey(
        PaperSection, verbose_name="所属大题", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="paper_questions",
    )
    question = models.ForeignKey(
        "questions.Question", verbose_name="题目", on_delete=models.PROTECT,
        related_name="paper_usages",
    )

    order = models.PositiveIntegerField("全卷顺序", default=0, help_text="从 1 开始，跨大题连续编号",
                                        db_index=True)
    score = models.DecimalField("本卷分值", max_digits=6, decimal_places=2)

    # ---- 快照字段（见类文档）----
    question_type = models.CharField("题型（快照）", max_length=16, choices=QuestionType.choices,
                                     db_index=True)
    difficulty = models.PositiveSmallIntegerField("难度（快照）", choices=Difficulty.choices,
                                                  default=Difficulty.MEDIUM, db_index=True)

    class Meta:
        db_table = "paper_question"
        verbose_name = "试卷题目"
        verbose_name_plural = "试卷题目"
        ordering = ["paper", "order", "id"]
        constraints = [
            # 同一份卷子里不允许重复放同一道题（教师想考两次同一个知识点，应该换一道题）
            models.UniqueConstraint(fields=["paper", "question"], name="uniq_paper_question"),
        ]
        indexes = [
            models.Index(fields=["paper", "order"], name="idx_pq_paper_order"),
            models.Index(fields=["section", "order"], name="idx_pq_section_order"),
        ]

    def __str__(self):
        return f"{self.paper_id}#{self.order}"

    # ------------------------------------------------------------------
    def sync_from_question(self) -> None:
        """从题库题目刷新快照字段（新增时调用一次）。"""
        self.question_type = self.question.type
        self.difficulty = self.question.difficulty

    def score_float(self) -> float:
        return float(self.score or 0)

    def student_payload(self) -> dict:
        """交给考试端的题目内容——**不含答案**，直接委托给 Question.student_view()。"""
        payload = self.question.student_view()
        payload["order"] = self.order
        payload["score"] = self.score_float()
        payload["section_id"] = self.section_id
        return payload


__all__ = ["Paper", "PaperQuerySet", "PaperQuestion", "PaperSection"]
