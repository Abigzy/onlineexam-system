"""作弊行为检测模块的模型。

模块八**只新增一张表** ``ExamBehaviorLog``：逐条行为埋点。

为什么模块四不建这张表、偏偏留到模块八？
    模块四关心的是「当场发现并处置」——所以只在 ``ExamAttempt`` 上做聚合计数
    （``switch_count`` / ``paste_count`` / ``fullscreen_exit_count``），
    够用来做「超限强制交卷」了。而模块八关心的是「事后复盘与举证」——
    需要知道**每一次**切屏发生在第几分钟、离开了多久、当时正在做哪道题。
    两个需求的数据粒度不同，硬塞进一张表反而两头不讨好。

    **这张表只存事实，不存结论。** 切屏了几次、离开了几秒是客观事实；
    「算不算可疑」依赖阈值，而阈值是可配的（教师可能把允许切屏次数从 3 调到 5）。
    如果判定结果在写入时就算好落库，教师一改阈值，历史结论立刻过时。
    所以结论一律由 ``RuleEngineService`` **读的时候现算**——
    与模块六「学情统计是只读模型」同一个思路。

与 ``ExamAttempt`` 上聚合计数的一致性
    埋点写在小模块里（``BehaviorLogService``），落库的是前端上报的**原始事件**；
    聚合计数仍然由模块四维护。为了让两边的数字对得上，
    ``blur``（窗口失焦）在计数口径里也算进 ``switch_count``——
    详情页的核对逻辑要按同一口径聚合（见 ``SWITCH_LIKE_EVENTS``）。
"""

import json
import logging

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.common.models import TimeStampedModel

from .constants import BehaviorEvent

logger = logging.getLogger("apps.proctoring")


class ExamBehaviorLogQuerySet(models.QuerySet):
    """行为埋点查询集：把常用的过滤口径收在一处，别在视图里拼长链。"""

    def of_attempt(self, attempt):
        return self.filter(attempt=attempt)

    def of_exam(self, exam):
        return self.filter(exam=exam)

    def of_student(self, student):
        return self.filter(student=student)

    def of_type(self, event_type):
        return self.filter(event_type=event_type)

    def analyzed(self):
        """只取参与行为分析的事件类型（心跳不入内）。"""
        from .constants import ANALYZED_EVENT_TYPES

        return self.filter(event_type__in=ANALYZED_EVENT_TYPES)

    def with_relations(self):
        return self.select_related("attempt", "exam", "student", "question")

    def recent_first(self):
        return self.order_by("-occurred_at", "-id")


class ExamBehaviorLog(TimeStampedModel):
    """一条行为埋点（切屏 / 粘贴 / 退出全屏 / 失焦）。

    一条记录 = 前端一次上报里的一种事件（带 ``count``），而不是「一次点击一行」——
    前端本来就按批上报，逐条展开会让日志量膨胀十倍而信息量不变。
    """

    attempt = models.ForeignKey(
        "exam.ExamAttempt", verbose_name="作答记录", on_delete=models.CASCADE,
        related_name="behavior_logs",
    )
    # 冗余 exam / student：看板都是「按考试筛、按学生聚合」，多这两列能少两次 join
    exam = models.ForeignKey(
        "exam.Exam", verbose_name="考试", on_delete=models.CASCADE,
        related_name="behavior_logs",
    )
    student = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="学生", on_delete=models.CASCADE,
        related_name="behavior_logs",
    )

    event_type = models.CharField("事件类型", max_length=32, choices=BehaviorEvent.choices,
                                  db_index=True)
    count = models.PositiveIntegerField("本次条数", default=1)
    # 事件发生时间：默认取服务端时间（前端时钟不可信，和计时权威在服务端同理）
    occurred_at = models.DateTimeField("发生时间", default=timezone.now, db_index=True)
    # 离开/停留时长（秒）：切屏、失焦事件才有；0 表示未上报
    duration_seconds = models.PositiveIntegerField("离开/停留时长（秒）", default=0)

    # 事件发生时所处的题目（前端能带上就带上，便于把切屏与具体题目对应起来）
    question = models.ForeignKey(
        "questions.Question", verbose_name="当时作答的题目", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="behavior_logs",
    )
    question_index = models.PositiveIntegerField("题号（第几题）", default=0)

    # 环境快照：同一次作答里比对它们就能发现「换设备/换网络」
    ip_address = models.CharField("IP", max_length=45, blank=True, default="")
    user_agent = models.CharField("User-Agent", max_length=255, blank=True, default="")

    # 扩展信息（JSON 字符串——MySQL 5.6 无原生 JSON 字段，与全项目一致用 TextField）
    detail = models.TextField("扩展信息（JSON）", blank=True, default="")

    objects = ExamBehaviorLogQuerySet.as_manager()

    class Meta:
        db_table = "proctoring_behavior_log"
        verbose_name = "行为埋点"
        verbose_name_plural = "行为埋点"
        ordering = ["-occurred_at", "-id"]
        indexes = [
            # 证据链页面：按作答取时间线
            models.Index(fields=["attempt", "-occurred_at"], name="idx_blog_attempt_time"),
            # 看板：按考试 + 事件类型聚合
            models.Index(fields=["exam", "event_type"], name="idx_blog_exam_type"),
            models.Index(fields=["exam", "-occurred_at"], name="idx_blog_exam_time"),
            models.Index(fields=["student", "-occurred_at"], name="idx_blog_student_time"),
        ]

    def __str__(self):
        return f"attempt#{self.attempt_id} {self.event_type}x{self.count}"

    # ------------------------------------------------------------------
    def detail_dict(self) -> dict:
        """解析扩展信息。失败返回空字典而不是抛异常——
        一条脏埋点不该让整条证据链页面 500。"""
        if not self.detail:
            return {}
        try:
            data = json.loads(self.detail)
        except (TypeError, ValueError) as exc:  # noqa: BLE001
            logger.warning("行为埋点 #%s 的 JSON 解析失败：%s", self.id, exc)
            return {}
        return data if isinstance(data, dict) else {}

    def to_payload(self) -> dict:
        """给证据链页面用的可渲染结构。"""
        return {
            "id": self.id,
            "attempt_id": self.attempt_id,
            "event_type": self.event_type,
            "event_display": self.get_event_type_display(),
            "count": self.count,
            "occurred_at": (timezone.localtime(self.occurred_at).strftime("%Y-%m-%d %H:%M:%S")
                            if self.occurred_at else ""),
            "duration_seconds": self.duration_seconds,
            "question_index": self.question_index,
            "question_id": self.question_id,
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
            "detail": self.detail_dict(),
        }


__all__ = ["ExamBehaviorLog", "ExamBehaviorLogQuerySet"]
