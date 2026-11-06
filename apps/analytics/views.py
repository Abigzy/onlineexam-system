"""学情分析接口。

路由总览（``/api/v1/`` 下）：

    教师（班级维度）
    GET    /analytics/exams/                     可分析的考试列表（页面选择器）
    GET    /analytics/exams/{id}/report/         班级学情总览（分布 / 参与度 / 薄弱点）
    GET    /analytics/exams/{id}/questions/      逐题得分率与区分度

    学生维度（教师可看任何人；学生**只能看自己**）
    GET    /analytics/students/{id}/mastery/     知识点掌握度矩阵
    GET    /analytics/students/{id}/trend/       成绩趋势（含线性回归预测）
    GET    /analytics/students/{id}/report/      个人学情总览（雷达 / 趋势 / 诊断）
    POST   /analytics/students/{id}/diagnosis/   生成 / 刷新 AI 学情诊断

权限与模块五同源：
  * 班级维度是教师专属；
  * 学生维度的「跨人」返回 **404 而不是 403**——「这个学生 id 存不存在」
    不该成为可以探测的信息；
  * 学生可以为自己生成 AI 诊断（是本人的数据、且命中缓存就不会重复计费），
    但没有任何接口能让他看到别人的学情。
"""

import logging

from django.contrib.auth import get_user_model
from django.http import Http404
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.common.constants import Role
from apps.common.permissions import IsTeacher
from apps.common.response import success

from .scoping import can_view_student, visible_student_ids
from apps.exam.models import Exam

from .constants import TREND_DEFAULT_LIMIT
from .serializers import (
    AnalyticsQuerySerializer,
    DiagnosisRequestSerializer,
    ExamReportQuerySerializer,
)
from .services import AIAnalysisService, AnalyticsReportService

logger = logging.getLogger("apps.analytics.views")


def _get_student(student_id, user):
    """取学生，并做对象级权限校验。

    口径见 ``apps.analytics.scoping``：管理员可看任何学生；教师只能看**自己带的班**
    （自己创建 / 任班主任 / 授课）里的学生；学生只能看自己。

    拒绝一律 404 —— 跨人资源不拿 403 当 id 探针；同时顺带拦掉「target 不是学生」的
    情况，避免把「这个 id 是谁」暴露出去。
    """
    student = get_object_or_404(get_user_model(), pk=student_id)
    if student.role != Role.STUDENT:
        raise Http404("学生不存在")
    if not can_view_student(user, student):
        logger.warning(
            "拒绝越权查看学情：user=%s(role=%s) target=%s",
            getattr(user, "username", "?"), getattr(user, "role", "?"), student_id,
        )
        raise Http404("学生不存在")
    return student


def _get_exam(exam_id: int) -> Exam:
    return get_object_or_404(Exam.objects.select_related("paper"), pk=exam_id)


def _clean_query(serializer_class, params) -> dict:
    serializer = serializer_class(data=params or {})
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


def _subject_of(params) -> str:
    """从查询参数里取学科，顺手做长度与空白处理。"""
    value = (params.get("subject") or "").strip()
    return value[:64]


# ==================================================================
# 班级维度（教师）
# ==================================================================
@extend_schema(tags=["学情分析 · 教师"])
class AnalyticsExamListView(APIView):
    """可分析的考试列表（有作答记录的考试）。"""

    permission_classes = [IsTeacher]

    @extend_schema(summary="可分析的考试列表")
    def get(self, request):
        rows = AnalyticsReportService.exam_options(request.user)
        return success(data={"count": len(rows), "results": rows})


@extend_schema(tags=["学情分析 · 教师"])
class ExamAnalyticsView(APIView):
    """班级学情总览：分数分布、参与度、行为概览、薄弱知识点、排名预览。"""

    permission_classes = [IsTeacher]

    @extend_schema(
        summary="班级学情总览",
        parameters=[
            OpenApiParameter("include_rows", bool, description="是否附带逐人明细"),
            OpenApiParameter("classroom_id", int, description="按班级过滤（可选）"),
        ],
    )
    def get(self, request, exam_id):
        exam = _get_exam(exam_id)
        data = _clean_query(ExamReportQuerySerializer, request.query_params)
        report = AnalyticsReportService.class_report(
            exam, include_rows=bool(data.get("include_rows")),
            student_ids=_classroom_student_ids(data.get("classroom_id"), request.user),
        )
        _mark_viewable(report, request.user)
        return success(data=report)


@extend_schema(tags=["学情分析 · 教师"])
class ExamQuestionAnalyticsView(APIView):
    """逐题得分率与区分度。"""

    permission_classes = [IsTeacher]

    @extend_schema(summary="逐题得分率与区分度", parameters=[
        OpenApiParameter("classroom_id", int, description="按班级过滤（可选）"),
    ])
    def get(self, request, exam_id):
        from .services import ClassAnalysisService

        exam = _get_exam(exam_id)
        student_ids = _classroom_student_ids(request.query_params.get("classroom_id"), request.user)
        return success(data=ClassAnalysisService.question_analysis(exam, student_ids))


def _mark_viewable(report: dict, user) -> None:
    """给排行 / 明细行打上「当前用户能否查看该生学情」的标记。

    标记只用于前端决定「要不要渲染成链接」，真正的拦截仍在 ``_get_student``；
    否则跨班考试时排行榜里会出现一堆点进去 404 的名字。
    """
    ranking = report.get("ranking") or {}
    if getattr(user, "is_admin", False):
        rows = list(report.get("rows") or [])
        for key in ("top", "bottom"):
            rows += list(ranking.get(key) or [])
        for row in rows:
            row["can_view_analytics"] = True
        return

    visible = set(visible_student_ids(user))
    for row in report.get("rows") or []:
        row["can_view_analytics"] = row.get("student_id") in visible
    for key in ("top", "bottom"):
        for row in ranking.get(key) or []:
            row["can_view_analytics"] = row.get("student_id") in visible


def _classroom_student_ids(classroom_id, user=None):
    """把 ``classroom_id`` 解析成学生 id 列表，并把范围限制在当前用户可见的班级内。

    * 传了 ``classroom_id``：必须是当前用户能管理的班，否则 404
      （不拿 403 去探测「这个班级存不存在」）；
    * 没传：教师只能统计**自己带的班**里的学生；管理员返回 ``None`` 表示不限。

    注意 ``student_ids=[]`` 与 ``None`` 语义不同：前者是「一个都看不到」，
    服务层用 ``if student_ids is not None`` 判断，所以空列表会正确产出空统计，
    不会退化成「全校可见」。
    """
    if classroom_id is None or str(classroom_id).strip() == "":
        if user is None or getattr(user, "is_admin", False):
            return None
        return list(visible_student_ids(user))

    from apps.classroom.models import Classroom

    try:
        cid = int(classroom_id)
    except (TypeError, ValueError):
        raise Http404("班级不存在")
    queryset = Classroom.objects.all() if user is None else Classroom.objects.manageable_by(user)
    classroom = queryset.filter(pk=cid).first()
    if classroom is None:
        raise Http404("班级不存在")
    return list(classroom.students.values_list("id", flat=True))


# ==================================================================
# 学生维度
# ==================================================================
@extend_schema(tags=["学情分析"])
class StudentMasteryView(APIView):
    """知识点掌握度矩阵与薄弱点。"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="知识点掌握度（含雷达图数据与薄弱点）",
        parameters=[OpenApiParameter("subject", str, description="按学科过滤")],
    )
    def get(self, request, student_id):
        from .services import KnowledgeMasteryService

        student = _get_student(student_id, request.user)
        return success(data=KnowledgeMasteryService.student_mastery(
            student, _subject_of(request.query_params),
        ))


@extend_schema(tags=["学情分析"])
class StudentTrendView(APIView):
    """成绩趋势与线性回归预测。"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="成绩趋势（含回归预测）",
        parameters=[
            OpenApiParameter("subject", str, description="按学科过滤"),
            OpenApiParameter("limit", int, description="取最近多少次作答"),
        ],
    )
    def get(self, request, student_id):
        from .services import TrendService

        student = _get_student(student_id, request.user)
        data = _clean_query(AnalyticsQuerySerializer, request.query_params)
        return success(data=TrendService.student_trend(
            student,
            subject=_subject_of(request.query_params),
            limit=data.get("limit") or TREND_DEFAULT_LIMIT,
        ))


@extend_schema(tags=["学情分析"])
class StudentAnalyticsView(APIView):
    """个人学情总览（页面一次拿全）。

    **不会自动调大模型**——只返回已有的诊断缓存（若有）。
    要生成诊断请调 ``POST .../diagnosis/``。
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="个人学情总览",
        parameters=[
            OpenApiParameter("subject", str, description="按学科过滤"),
            OpenApiParameter("limit", int, description="趋势取最近多少次作答"),
        ],
    )
    def get(self, request, student_id):
        student = _get_student(student_id, request.user)
        data = _clean_query(AnalyticsQuerySerializer, request.query_params)
        return success(data=AnalyticsReportService.student_report(
            student,
            subject=_subject_of(request.query_params),
            trend_limit=data.get("limit") or TREND_DEFAULT_LIMIT,
        ))


@extend_schema(tags=["学情分析"])
class StudentDiagnosisView(APIView):
    """生成 / 刷新 AI 学情诊断（命中缓存则不重复调用大模型）。"""

    permission_classes = [IsAuthenticated]

    @extend_schema(summary="生成 AI 学情诊断", request=DiagnosisRequestSerializer)
    def post(self, request, student_id):
        student = _get_student(student_id, request.user)
        serializer = DiagnosisRequestSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        result = AIAnalysisService.diagnose(
            student,
            subject=(data.get("subject") or "").strip()[:64],
            force=bool(data.get("force")),
            user=request.user,
            provider=data.get("provider") or None,
        )

        if result.get("cached"):
            message = "已返回缓存中的学情诊断"
        elif result.get("degraded"):
            message = "AI 暂不可用，已用本地规则生成学情诊断"
        elif result.get("is_mock"):
            message = "学情诊断已生成（当前使用本地模拟，未调用真实大模型）"
        else:
            message = "学情诊断已生成"
        return success(data=result, message=message)


__all__ = [
    "AnalyticsExamListView",
    "ExamAnalyticsView",
    "ExamQuestionAnalyticsView",
    "StudentAnalyticsView",
    "StudentDiagnosisView",
    "StudentMasteryView",
    "StudentTrendView",
]
