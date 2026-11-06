"""判分与成绩模块的模型。

模块五**只新增一张表**：``GradingFeedback``（教师复核记录）。

为什么不给「判分结果」再建一张表？
    模块四的 ``apps.exam.Answer`` 已经把判分字段都留好了——``is_correct`` /
    ``score`` / ``ai_score`` / ``ai_comment`` / ``teacher_comment``，
    再建一张「判分结果表」就是把同一份数据存两处，迟早对不上。

那缺的是什么？缺一份**审计痕迹**：这道题 AI 给了多少、教师最后改成多少、
是直接采纳还是自己重判。这份痕迹有两个用途：
    1. 教学复盘——「这个知识点的题我为什么普遍给得比 AI 低」；
    2. **AI 校准**——用「教师最终分」当基准，统计 AI 建议的平均绝对偏差与采纳率，
       这是「AI 辅助批改到底靠不靠谱」唯一有说服力的度量。

所以这张表的字段几乎都是**快照**（``ai_score`` / ``original_score``）：复核之后
``Answer`` 上的值会被覆盖，若不在反馈里留一份原值，历史就查不回来了。
"""

import logging

from django.conf import settings
from django.db import models

from apps.common.models import TimeStampedModel
from apps.exam.models import Answer, ExamAttempt

from .constants import GradeSource, ReviewAction

logger = logging.getLogger("apps.grading")


class GradingFeedbackQuerySet(models.QuerySet):
    """复核记录查询集。"""

    def of_attempt(self, attempt):
        return self.filter(attempt=attempt)

    def of_answer(self, answer):
        return self.filter(answer=answer)

    def of_reviewer(self, user):
        return self.filter(reviewer=user) if user is not None else self

    def with_ai_suggestion(self):
        """只取「有 AI 建议分可比对」的记录——校准统计的样本口径。"""
        return self.filter(ai_score__isnull=False)

    def accepted(self):
        return self.filter(action=ReviewAction.ACCEPT)

    def with_relations(self):
        return self.select_related(
            "answer__paper_question__question", "attempt__exam", "reviewer",
        )


class GradingFeedback(TimeStampedModel):
    """一次教师复核的完整记录。

    一条记录回答四个问题：**谁**（``reviewer``）在**哪道题**（``answer``）上，
    把分数从**多少**（``original_score``）改成了**多少**（``final_score``），
    以及 AI 原本建议了**多少**（``ai_score``）。
    """

    answer = models.ForeignKey(
        Answer, verbose_name="作答明细", on_delete=models.CASCADE,
        related_name="feedbacks",
    )
    # 冗余一份 attempt：批改工作台主要按「一场考试 / 一次作答」聚合，
    # 每次 JOIN 回 answer 再回 attempt 太绕，直接落一个外键更省事。
    attempt = models.ForeignKey(
        ExamAttempt, verbose_name="作答记录", on_delete=models.CASCADE,
        related_name="grading_feedbacks",
    )
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="复核人", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="grading_feedbacks",
    )

    question_type = models.CharField("题型（快照）", max_length=16, blank=True, default="")
    source = models.CharField(
        "原评分依据", max_length=8, choices=GradeSource.choices,
        default=GradeSource.NONE, help_text="复核之前这个分数是谁给的",
    )
    action = models.CharField("处置方式", max_length=16, choices=ReviewAction.choices,
                              default=ReviewAction.OVERRIDE)

    # ---- 三个分数快照（顺序就是复核的心路历程）----
    ai_score = models.DecimalField("AI 建议分（快照）", max_digits=6, decimal_places=2,
                                   null=True, blank=True)
    ai_ratio = models.FloatField("AI 得分比例（快照）", null=True, blank=True)
    original_score = models.DecimalField("复核前分数（快照）", max_digits=6, decimal_places=2,
                                         default=0)
    final_score = models.DecimalField("复核后分数", max_digits=6, decimal_places=2, default=0)
    delta = models.DecimalField("本次调整量", max_digits=6, decimal_places=2, default=0,
                                help_text="final_score - original_score，正数表示上调")

    full_score = models.DecimalField("本题满分（快照）", max_digits=6, decimal_places=2,
                                     default=0)
    comment = models.TextField("教师评语", blank=True, default="")

    objects = GradingFeedbackQuerySet.as_manager()

    class Meta:
        db_table = "grading_feedback"
        verbose_name = "复核记录"
        verbose_name_plural = "复核记录"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["answer", "-created_at"], name="idx_gfb_answer"),
            models.Index(fields=["attempt", "-created_at"], name="idx_gfb_attempt"),
            models.Index(fields=["reviewer", "-created_at"], name="idx_gfb_reviewer"),
        ]

    def __str__(self):
        return f"复核#{self.id} 作答{self.attempt_id}-题{self.answer_id}"

    # ------------------------------------------------------------------
    # 便捷属性
    # ------------------------------------------------------------------
    @property
    def ai_delta(self):
        """教师最终分与 AI 建议分的差——校准统计的核心指标。

        AI 没给过建议分时返回 ``None``（不是 0！0 会被误当成「完全一致」）。
        """
        if self.ai_score is None:
            return None
        return self.final_score - self.ai_score

    def to_payload(self) -> dict:
        return {
            "id": self.id,
            "answer_id": self.answer_id,
            "attempt_id": self.attempt_id,
            "reviewer_id": self.reviewer_id,
            "reviewer_name": getattr(self.reviewer, "display_name", "") or "",
            "question_type": self.question_type,
            "source": self.source,
            "action": self.action,
            "ai_score": float(self.ai_score) if self.ai_score is not None else None,
            "ai_ratio": self.ai_ratio,
            "original_score": float(self.original_score or 0),
            "final_score": float(self.final_score or 0),
            "delta": float(self.delta or 0),
            "ai_delta": float(self.ai_delta) if self.ai_delta is not None else None,
            "full_score": float(self.full_score or 0),
            "comment": self.comment,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S") if self.created_at else "",
        }


__all__ = ["GradingFeedback", "GradingFeedbackQuerySet"]
