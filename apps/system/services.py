"""
管理控制台的只读聚合服务。

设计纪律（PRD §3.4 / AC-36）：
  * **全部统计在 SQL 层完成**——只用 ``aggregate`` / ``values().annotate()``，
    绝不把整表拉到 Python 里数；
  * **查询次数恒定**——不随数据量增长（用 ``apps.system.tests`` 的
    ``CaptureQueriesContext`` 对比「1 条数据 vs 多条数据」的查询条数来锁定）；
  * **不暴露密钥**——Provider 只回「是否已配置」，不回 api_key 明文（AC-32）；
  * 本服务只读，不做任何写入，也不引入模型（因此零迁移）。

数据源：users / classroom / question / paper / exam(+attempt) / ai_request_log。
"""

import logging
import platform
from datetime import timedelta

import django
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.ai.models import AIRequestLog
from apps.ai.providers import catalog
from apps.classroom.constants import ClassType
from apps.classroom.models import Classroom
from apps.common.constants import ExamStatus, QuestionType, Role
from apps.exam.models import Exam, ExamAttempt
from apps.papers.constants import PaperStatus
from apps.papers.models import Paper
from apps.questions.constants import QuestionSource
from apps.questions.models import Question

logger = logging.getLogger("apps.system.services")

User = get_user_model()

# 「近 7 日」口径统一从这里取，避免各处写 7 / 6 不一致
RECENT_DAYS = 7


class SystemOverviewService:
    """管理端数据总览 / 组织概览 / 系统配置 的只读聚合。"""

    # ==================================================================
    # 一、数据总览（GET /api/v1/system/overview/ 与 /manage/ 共用同一份口径）
    # ==================================================================
    @classmethod
    def overview(cls) -> dict:
        """一次性返回四组指标 + 两张图的序列 + 候选项。

        返回结构（前端直接渲染，不再二次计算）::

            {
              "generated_at": "2026-10-21 15:00:00",
              "recent_days": 7,
              "users":     {...},   # 用户结构
              "org":       {...},   # 组织
              "content":   {...},   # 教学资源（含按题型分布）
              "teaching":  {...},   # 考试与作答（含按状态分布）
              "ai":        {...},   # AI 用量（含 provider 现状与 7 日趋势）
              "options":   {...},   # 学科候选
            }

        两张图的数据分别取自 ``teaching.exams_by_status`` 与 ``ai.trend``，
        都是固定长度的列表（5 个状态 / 7 天），与数据量无关。
        """
        since = timezone.now() - timedelta(days=RECENT_DAYS)
        return {
            "generated_at": timezone.localtime().strftime("%Y-%m-%d %H:%M:%S"),
            "recent_days": RECENT_DAYS,
            "users": cls._user_metrics(since),
            "org": cls._org_metrics(),
            "content": cls._content_metrics(),
            "teaching": cls._teaching_metrics(since),
            "ai": cls._ai_metrics(since),
            "options": {"subjects": cls.subject_options()},
        }

    # ---------------- 用户 ----------------
    @staticmethod
    def _user_metrics(since) -> dict:
        """用户结构。

        口径说明（P3-1，**不要改成互相排斥**）：各角色桶是**并列维度、可能重叠、
        不可相加**——``admins`` 按 ``role='admin' OR is_superuser=True`` 统计，
        ``teachers``/``students`` 按 ``role`` 统计，因此当某个超管把 role 改成
        teacher 时，它会同时落在 ``admins`` 与 ``teachers`` 里，
        三个桶之和可能大于 ``total``。展示时请按桶分别读取，不要拿它们求百分比分母。
        """
        stats = User.objects.aggregate(
            total=Count("id"),
            inactive=Count("id", filter=Q(is_active=False)),
            new_recent=Count("id", filter=Q(date_joined__gte=since)),
            active_recent=Count("id", filter=Q(last_login__gte=since)),
            admins=Count("id", filter=Q(role=Role.ADMIN) | Q(is_superuser=True)),
            teachers=Count("id", filter=Q(role=Role.TEACHER)),
            students=Count("id", filter=Q(role=Role.STUDENT)),
            teachers_without_subject=Count("id", filter=Q(role=Role.TEACHER) & Q(subject="")),
        )
        return {
            "total": stats["total"] or 0,
            "by_role": {
                "admin": stats["admins"] or 0,
                "teacher": stats["teachers"] or 0,
                "student": stats["students"] or 0,
            },
            "inactive": stats["inactive"] or 0,
            "new_recent": stats["new_recent"] or 0,
            "active_recent": stats["active_recent"] or 0,
            "teachers_without_subject": stats["teachers_without_subject"] or 0,
        }

    # ---------------- 组织 ----------------
    @staticmethod
    def _org_metrics() -> dict:
        stats = Classroom.objects.aggregate(
            total=Count("id"),
            admin_classes=Count("id", filter=Q(class_type=ClassType.ADMIN)),
            subject_classes=Count("id", filter=Q(class_type=ClassType.SUBJECT)),
            archived=Count("id", filter=Q(is_active=False)),
        )
        return {
            "classrooms": stats["total"] or 0,
            "admin_classes": stats["admin_classes"] or 0,
            "subject_classes": stats["subject_classes"] or 0,
            "archived_classrooms": stats["archived"] or 0,
        }

    # ---------------- 内容 ----------------
    @staticmethod
    def _content_metrics() -> dict:
        stats = Question.objects.aggregate(
            total=Count("id"),
            active=Count("id", filter=Q(is_active=True)),
            ai_generated=Count("id", filter=Q(source=QuestionSource.AI)),
        )
        # 按题型分组：行数上限 = 题型种类数（6），与数据量无关
        by_type_rows = (
            Question.objects.values("type").annotate(total=Count("id")).order_by("-total")
        )
        by_type = {row["type"]: row["total"] for row in by_type_rows}

        paper_stats = Paper.objects.aggregate(
            total=Count("id"),
            published=Count("id", filter=Q(status=PaperStatus.PUBLISHED)),
        )
        return {
            "questions": stats["total"] or 0,
            "questions_active": stats["active"] or 0,
            "questions_ai": stats["ai_generated"] or 0,
            "questions_by_type": [
                {
                    "type": value,
                    "label": str(QuestionType(value).label) if value else "未知",
                    "total": by_type.get(value, 0),
                }
                for value in QuestionType.values
            ],
            "papers": paper_stats["total"] or 0,
            "papers_published": paper_stats["published"] or 0,
        }

    # ---------------- 教学 ----------------
    @staticmethod
    def _teaching_metrics(since) -> dict:
        status_rows = (
            Exam.objects.values("status").annotate(total=Count("id")).order_by("-total")
        )
        # 固定输出五个状态，图表不会因为某状态为 0 而缺项
        by_status_map = {row["status"]: row["total"] for row in status_rows}
        by_status = [
            {
                "status": value,
                "label": str(ExamStatus(value).label) if value else "未知",
                "total": by_status_map.get(value, 0),
            }
            for value in ExamStatus.values
        ]

        # 单次聚合同时算出「近 7 日参考」与「近 7 日出分」，避免两次全表扫描
        attempt_stats = ExamAttempt.objects.aggregate(
            attempts_recent=Count("id", filter=Q(started_at__gte=since)),
            graded_recent=Count("id", filter=Q(graded_at__gte=since)),
            attempts_total=Count("id"),
        )
        return {
            "exams": sum(by_status_map.values()),
            "exams_by_status": by_status,
            "attempts_recent": attempt_stats["attempts_recent"] or 0,
            "graded_recent": attempt_stats["graded_recent"] or 0,
            "attempts_total": attempt_stats["attempts_total"] or 0,
        }

    # ---------------- AI 用量 ----------------
    @classmethod
    def _ai_metrics(cls, since) -> dict:
        recent = AIRequestLog.objects.filter(created_at__gte=since)
        stats = recent.aggregate(
            calls=Count("id"),
            failed=Count("id", filter=Q(success=False)),
            mock_calls=Count("id", filter=Q(is_mock=True)),
            total_tokens=Sum("total_tokens"),
        )
        calls = stats["calls"] or 0
        failed = stats["failed"] or 0
        return {
            "calls_recent": calls,
            "failed_recent": failed,
            "fail_rate": round(failed * 100.0 / calls, 1) if calls else 0.0,
            "tokens_recent": stats["total_tokens"] or 0,
            "mock_calls_recent": stats["mock_calls"] or 0,
            "trend": cls.daily_trend(),
            "provider": cls.provider_status(),
        }

    @staticmethod
    def daily_trend(days: int = RECENT_DAYS) -> list:
        """近 N 日 AI 调用次数（按天）。

        刻意**不用** ``TruncDate`` / ``__date``：它们会生成 ``DATE(CONVERT_TZ(...))``，
        而 MySQL 5.6 若未导入时区表会返回 NULL，趋势图会整片空白。
        这里按「日边界」切成 N 个区间分别 count，SQL 只有普通的 >= / < 比较，
        跨库（SQLite / MySQL 5.6）行为一致；查询数固定为 N，不随数据量增长。
        """
        days = max(int(days or RECENT_DAYS), 1)
        today = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
        trend = []
        for offset in range(days - 1, -1, -1):
            start = today - timedelta(days=offset)
            end = start + timedelta(days=1)
            calls = AIRequestLog.objects.filter(
                created_at__gte=start, created_at__lt=end
            ).count()
            trend.append({"date": start.strftime("%m-%d"), "calls": calls})
        return trend

    # ==================================================================
    # 二、候选项
    # ==================================================================
    @staticmethod
    def subject_options() -> list:
        """库内已有学科的去重列表（用户 + 班级 + 题库三处并集）。

        ``.order_by()`` 是必需的：User/Question 的 Meta 有默认排序，
        与 ``distinct()`` 组合会让 ORDER BY 的列进入 SELECT，去重失效。
        """
        subjects = set()
        for queryset in (
            User.objects.exclude(subject="").order_by().values_list("subject", flat=True),
            Classroom.objects.exclude(subject="").order_by().values_list("subject", flat=True),
            Question.objects.exclude(subject="").order_by().values_list("subject", flat=True),
        ):
            subjects.update(value.strip() for value in queryset if value and value.strip())
        return sorted(subjects)

    # ==================================================================
    # 三、组织与基础数据（/manage/org/）
    # ==================================================================
    @classmethod
    def org_snapshot(cls, classroom_limit: int = 200, teacher_limit: int = 100) -> dict:
        """班级一览 + 学科分布 + 未绑定学科的教师清单（全部只读）。"""
        classrooms = (
            Classroom.objects.annotate(
                student_total=Count("students", distinct=True),
                teacher_total=Count("teachers", distinct=True),
            )
            .select_related("head_teacher")
            .order_by("-created_at")[:classroom_limit]
        )

        # 学科分布：题量 / 教师数 / 考试数，三张表各自分组后在 Python 里按学科合并
        question_by_subject = {
            row["subject"]: row["total"]
            for row in Question.objects.exclude(subject="")
            .order_by()
            .values("subject")
            .annotate(total=Count("id"))
        }
        teacher_by_subject = {
            row["subject"]: row["total"]
            for row in User.objects.filter(role=Role.TEACHER)
            .exclude(subject="")
            .order_by()
            .values("subject")
            .annotate(total=Count("id"))
        }
        exam_by_subject = {
            row["subject"]: row["total"]
            for row in Exam.objects.exclude(subject="")
            .order_by()
            .values("subject")
            .annotate(total=Count("id"))
        }

        subjects = sorted(
            set(question_by_subject) | set(teacher_by_subject) | set(exam_by_subject),
            key=lambda name: -(question_by_subject.get(name, 0)),
        )
        subject_rows = [
            {
                "subject": name,
                "questions": question_by_subject.get(name, 0),
                "teachers": teacher_by_subject.get(name, 0),
                "exams": exam_by_subject.get(name, 0),
            }
            for name in subjects
        ]

        unbound_teachers = list(
            User.objects.filter(role=Role.TEACHER, is_active=True, subject="")
            .order_by("-date_joined")
            .values("id", "username", "real_name", "email")[:teacher_limit]
        )

        return {
            "classrooms": list(classrooms),
            "classroom_total": Classroom.objects.count(),
            "admin_class_total": Classroom.objects.filter(class_type=ClassType.ADMIN).count(),
            "subject_class_total": Classroom.objects.filter(class_type=ClassType.SUBJECT).count(),
            "subjects": subject_rows,
            "unbound_teachers": unbound_teachers,
            "unbound_teacher_total": User.objects.filter(
                role=Role.TEACHER, is_active=True, subject=""
            ).count(),
        }

    # ==================================================================
    # 四、系统配置（/manage/system/）
    # ==================================================================
    @classmethod
    def ai_usage(cls) -> dict:
        """AI 用量摘要（系统配置页用；口径与总览完全一致，避免两套数字）。"""
        return cls._ai_metrics(timezone.now() - timedelta(days=RECENT_DAYS))

    @staticmethod
    def provider_status() -> dict:
        """Provider 现状：**只回「是否已配置」，绝不回 api_key 明文**（AC-32）。"""
        current = (settings.AI_PROVIDER or "").lower()
        providers = catalog()
        for item in providers:
            item["is_current"] = item["key"] == current

        active_is_mock = _active_is_mock()
        return {
            "current": current,
            "allow_mock": bool(settings.AI_ALLOW_MOCK),
            "active_is_mock": active_is_mock,
            "providers": providers,
        }

    @staticmethod
    def system_info() -> dict:
        """系统信息（版本 / 数据库 / 时区），给运维与答辩做「环境快照」。"""
        return {
            "django_version": django.get_version(),
            "python_version": platform.python_version(),
            "database_vendor": connection.vendor,
            "database_version": _database_version(),
            "database_name": str(connection.settings_dict.get("NAME", "")),
            "time_zone": settings.TIME_ZONE,
            "debug": bool(settings.DEBUG),
            "recent_days": RECENT_DAYS,
        }


def _active_is_mock() -> bool:
    """当前生效的 provider 是否处于本地模拟。

    包了 try：``AI_ALLOW_MOCK=False`` 且未配 Key 时 ``build_provider`` 会抛错，
    此时系统实际上「不可用」，按 mock 处理并让页面显示警示条。
    """
    try:
        from apps.ai.client import get_ai_client

        return bool(get_ai_client().is_mock)
    except Exception as exc:  # noqa: BLE001 - 配置缺失不应让整页 500
        logger.warning("读取 AI Provider 状态失败，按未配置处理：%s", exc)
        return True


def _database_version() -> str:
    """尽力取数据库版本号；失败只回引擎名，不影响页面（AC-31 不报错）。"""
    try:
        with connection.cursor() as cursor:
            if connection.vendor == "sqlite":
                cursor.execute("select sqlite_version()")
            else:
                cursor.execute("select version()")
            row = cursor.fetchone()
        return str(row[0]) if row else connection.vendor
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取数据库版本失败：%s", exc)
        return connection.vendor


__all__ = ["RECENT_DAYS", "SystemOverviewService"]
