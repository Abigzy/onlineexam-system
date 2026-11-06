"""学情分析模块的模型。

模块六**只新增一张表**：``StudentAnalysisReport``。

为什么学情分析几乎不需要建表？
    掌握度、薄弱点、成绩趋势、班级分布全都是**从现有数据重新算出来的读模型**——
    数据源是模块五已经判好的 ``Answer.is_correct`` / ``Answer.score``
    加上模块四的 ``ExamAttempt``。把这些再存一份，就是「同一份结论存两处」，
    改了判分口径之后两份数据必然打架。

那这张表存什么？**存大模型的学情诊断结论。**
    和模块三把组卷说明存进 ``Paper.ai_notes`` 是同一个理由：调一次大模型要花钱、
    要等好几秒，而学生可能一节课内把学情页刷新十次。不缓存的话，同一份数据
    每次刷新都重问一遍模型，既贵又会让结论来回变化（模型不是确定性函数）。

    ``scope_key`` 是「统计范围」的稳定字符串（``all`` / ``subject:数据结构``），
    同一个学生在同一个范围下**只保留最新一份**诊断，避免历史报告越堆越多、
    页面上不知道该显示哪一份。
"""

import json
import logging
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.common.models import TimeStampedModel

logger = logging.getLogger("apps.analytics")


class StudentAnalysisReportQuerySet(models.QuerySet):
    """学情诊断报告查询集。"""

    def of_student(self, student):
        return self.filter(student=student)

    def for_scope(self, scope_key: str):
        return self.filter(scope_key=scope_key or "all")

    def with_relations(self):
        return self.select_related("student")

    def fresh(self, hours: int):
        """未过期的报告（页面直接命中缓存，不用花钱重算）。"""
        if not hours:
            return self
        return self.filter(generated_at__gte=timezone.now() - timedelta(hours=hours))


class StudentAnalysisReport(TimeStampedModel):
    """一个学生在某个统计范围下的 AI 学情诊断（缓存 + 留痕）。"""

    student = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="学生", on_delete=models.CASCADE,
        related_name="analysis_reports",
    )
    # 统计范围：「all」或「subject:数据结构」。做成字符串而不是外键，
    # 是因为范围以后可能长成「exam:12」「term:2026-秋」，不该被表结构框死。
    scope_key = models.CharField("统计范围标识", max_length=64, default="all", db_index=True)
    scope_display = models.CharField("统计范围描述", max_length=128, blank=True, default="全部学科")
    subject = models.CharField("学科", max_length=64, blank=True, default="")

    # ---- 结论（从 payload 里提上来几个常用字段，方便列表页与排序）----
    summary = models.TextField("整体学情概括", blank=True, default="")
    overall_level = models.CharField("总体水平", max_length=32, blank=True, default="")
    weak_point_count = models.PositiveIntegerField("薄弱知识点数", default=0)
    knowledge_point_count = models.PositiveIntegerField("参与分析的知识点数", default=0)
    sample_size = models.PositiveIntegerField("样本答题数", default=0)

    # ---- 完整结论（结构化 JSON 原文）----
    payload = models.TextField("诊断结果（JSON）", blank=True, default="")

    # ---- AI 溯源 ----
    provider = models.CharField("AI 平台", max_length=32, blank=True, default="")
    model = models.CharField("AI 模型", max_length=64, blank=True, default="")
    is_mock = models.BooleanField("本地模拟", default=False,
                                  help_text="未配置 API Key 时由本地模拟生成")
    generated_at = models.DateTimeField("生成时间", default=timezone.now, db_index=True)

    objects = StudentAnalysisReportQuerySet.as_manager()

    class Meta:
        db_table = "analytics_student_report"
        verbose_name = "学情诊断报告"
        verbose_name_plural = "学情诊断报告"
        ordering = ["-generated_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["student", "scope_key"],
                                    name="uniq_analysis_student_scope"),
        ]
        indexes = [
            models.Index(fields=["student", "scope_key"], name="idx_analysis_student_scope"),
            models.Index(fields=["-generated_at"], name="idx_analysis_generated"),
        ]

    def __str__(self):
        return f"{self.student_id}@{self.scope_key}"

    # ------------------------------------------------------------------
    def payload_dict(self) -> dict:
        """把 JSON 原文解析回字典。解析失败返回空字典而不是抛异常——
        页面宁可少显示一块诊断，也不该整个学情仪表盘 500。"""
        if not self.payload:
            return {}
        try:
            data = json.loads(self.payload)
        except (TypeError, ValueError) as exc:  # noqa: BLE001
            logger.warning("学情报告 #%s 的 JSON 解析失败：%s", self.id, exc)
            return {}
        return data if isinstance(data, dict) else {}

    def is_stale(self, hours: int) -> bool:
        """是否已过期（超过有效期就该重新生成）。"""
        if not hours or not self.generated_at:
            return False
        return self.generated_at < timezone.now() - timedelta(hours=hours)

    def to_payload(self, *, include_detail: bool = True) -> dict:
        data = {
            "id": self.id,
            "student_id": self.student_id,
            "scope_key": self.scope_key,
            "scope_display": self.scope_display,
            "subject": self.subject,
            "summary": self.summary,
            "overall_level": self.overall_level,
            "weak_point_count": self.weak_point_count,
            "knowledge_point_count": self.knowledge_point_count,
            "sample_size": self.sample_size,
            "provider": self.provider,
            "model": self.model,
            "is_mock": self.is_mock,
            "generated_at": (timezone.localtime(self.generated_at).strftime("%Y-%m-%d %H:%M:%S")
                             if self.generated_at else ""),
        }
        if include_detail:
            data["detail"] = self.payload_dict()
        return data


__all__ = ["StudentAnalysisReport", "StudentAnalysisReportQuerySet"]
