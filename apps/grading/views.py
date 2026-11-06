"""
判分与成绩接口。

路由总览（``/api/v1/`` 下）：

    判分（教师）
    POST   /grading/attempts/{id}/grade/      判分（客观题自动；with_ai=true 时加批主观题）
    POST   /grading/attempts/{id}/finalize/   出分（状态推进到「已出分」，学生即可见成绩）
    POST   /grading/answers/{id}/review/      复核一道题（改分 + 评语 / 一键采纳 AI 建议）

    批改工作台（教师）
    GET    /grading/queue/                    待批改队列（可按 exam_id 过滤）
    GET    /grading/exams/                    有作答的考试列表（工作台选择器）
    GET    /grading/attempts/{id}/report/     成绩单（教师全量 / 学生按公开规则脱敏）
    GET    /grading/feedback/                 复核记录列表（可按 attempt_id / answer_id 过滤）

    成绩与统计
    GET    /grading/scoreboard/               班级成绩单（排名 / 分数分布 / 及格率）
    GET    /grading/wrong-book/               错题本（学生看自己；教师可指定 student_id）
    GET    /grading/calibration/              AI 批改校准统计（采纳率 / 平均偏差 / 分档一致率）

权限原则：
  * 一切「写判分结果」的动作限教师；
  * 成绩单「谁能看什么」由 ``ScoreReportService`` 统一裁决——
    学生永远只能看自己的，且只有成绩公开后才拿得到逐题对错与答案。
"""

import logging

from django.http import Http404
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.common.exceptions import BusinessError
from apps.common.permissions import IsTeacher
from apps.common.response import success
from apps.exam.constants import AttemptStatus
from apps.exam.models import Answer, Exam, ExamAttempt

from .models import GradingFeedback
from .serializers import (
    AnswerReviewSerializer,
    AttemptFinalizeSerializer,
    AttemptGradeSerializer,
    GradingFeedbackSerializer,
)
from .services import GradingService, ScoreReportService

logger = logging.getLogger("apps.grading.views")


def _as_int(value, label: str) -> int:
    """把 query / body 里的「看起来是 id」的值转成整数。

    为什么要单独转一次：``get_object_or_404(Model, pk="abc")`` 里 Django 会在
    ``queryset.get()`` 阶段抛 ``ValueError: Field 'id' expected a number``，
    ``get_object_or_404`` 只捕 ``DoesNotExist``，这个 ``ValueError`` 会一路冒到
    异常处理器变成 **500**。参数写错是客户端问题，必须是 400。
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        raise BusinessError(f"{label} 必须是整数")


def _get_attempt(attempt_id: int, user) -> ExamAttempt:
    """取作答记录。

    教师（含管理员）可以看任何人的；学生**只能看自己的**——越权时返回 404 而不是 403：
    「这个资源不属于你」不必让对方知道它到底存不存在。
    """
    queryset = ExamAttempt.objects.select_related("exam", "exam__paper", "student")
    attempt = get_object_or_404(queryset, pk=_as_int(attempt_id, "attempt_id"))
    if getattr(user, "is_teacher", False):
        return attempt
    if attempt.student_id != user.id:
        raise Http404("作答记录不存在")
    return attempt


def _get_exam(exam_id) -> Exam:
    if not exam_id:
        raise BusinessError("请指定考试（exam_id）")
    return get_object_or_404(
        Exam.objects.select_related("paper"), pk=_as_int(exam_id, "exam_id"),
    )


# ======================================================================
# 判分
# ======================================================================
@extend_schema(tags=["判分 · 教师"])
class AttemptGradeView(APIView):
    """对一份作答判分。"""

    permission_classes = [IsTeacher]

    @extend_schema(summary="判分（客观题自动 / with_ai 时加 AI 批主观题）",
                   request=AttemptGradeSerializer)
    def post(self, request, attempt_id):
        attempt = get_object_or_404(
            ExamAttempt.objects.select_related("exam", "exam__paper"), pk=attempt_id,
        )
        serializer = AttemptGradeSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        result = GradingService.grade_attempt(
            attempt,
            user=request.user,
            with_ai=bool(data.get("with_ai")),
            policy=data.get("policy") or None,
            provider=data.get("provider") or None,
            force=bool(data.get("force")),
        )

        message = "客观题判分完成"
        if data.get("with_ai"):
            subjective = result.get("subjective") or {}
            message = f"判分完成：AI 批改 {subjective.get('graded', 0)} 道主观题"
            if subjective.get("failed"):
                message += f"，{subjective['failed']} 道需人工判分"
            if subjective.get("is_mock"):
                message += "（当前使用本地模拟，未调用真实大模型）"
        return success(data=result, message=message)


@extend_schema(tags=["判分 · 教师"])
class AttemptFinalizeView(APIView):
    """出分：把作答推进到「已出分」，学生从此可见成绩。"""

    permission_classes = [IsTeacher]

    @extend_schema(summary="出分", request=AttemptFinalizeSerializer)
    def post(self, request, attempt_id):
        attempt = get_object_or_404(
            ExamAttempt.objects.select_related("exam", "exam__paper"), pk=attempt_id,
        )
        serializer = AttemptFinalizeSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)

        attempt = GradingService.finalize_attempt(attempt, grader=request.user)
        report = ScoreReportService.attempt_report(attempt, viewer=request.user)
        return success(data=report, message="已出分，学生现在可以查看成绩")


@extend_schema(tags=["判分 · 教师"])
class AnswerReviewView(APIView):
    """教师复核一道题。"""

    permission_classes = [IsTeacher]

    @extend_schema(summary="复核一道题（改分 / 评语 / 一键采纳 AI 建议）",
                   request=AnswerReviewSerializer)
    def post(self, request, answer_id):
        answer = get_object_or_404(
            Answer.objects.select_related(
                "attempt__exam", "paper_question__question",
            ),
            pk=answer_id,
        )
        serializer = AnswerReviewSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        feedback = GradingService.review_answer(
            answer,
            reviewer=request.user,
            score=data.get("score"),
            comment=data.get("comment") or "",
            accept_ai=bool(data.get("accept_ai")),
            action=data.get("action") or None,
        )

        status = GradingService.grading_status(answer.attempt)
        message = "已采纳 AI 建议分" if data.get("accept_ai") else "已保存复核结果"
        return success(
            data={
                "feedback": GradingFeedbackSerializer(feedback).data,
                "answer": {
                    "id": answer.id,
                    "score": float(answer.score or 0),
                    "is_correct": answer.is_correct,
                    "teacher_comment": answer.teacher_comment,
                },
                "grading": status,
            },
            message=message,
        )


# ======================================================================
# 批改工作台
# ======================================================================
@extend_schema(tags=["判分 · 教师"])
class GradingQueueView(APIView):
    """待批改队列。"""

    permission_classes = [IsTeacher]

    @extend_schema(
        summary="待批改队列",
        parameters=[
            OpenApiParameter("exam_id", int, description="按考试过滤（必填）"),
            OpenApiParameter("only_pending", bool, description="只看还没批完的（默认 true）"),
        ],
    )
    def get(self, request):
        exam = _get_exam(request.query_params.get("exam_id"))
        raw = (request.query_params.get("only_pending") or "true").lower()
        only_pending = raw not in ("0", "false", "no")
        rows = GradingService.grading_queue(exam, only_pending=only_pending)
        return success(data={
            "exam": {
                "id": exam.id,
                "title": exam.title,
                "subject": exam.subject,
                # Exam.total_score 是属性（= 试卷满分），没有 total_score_float 这个方法
                "total_score": float(exam.total_score or 0),
                "question_count": exam.question_count,
                "multiple_choice_policy": exam.multiple_choice_policy,
                "multiple_choice_policy_display": exam.get_multiple_choice_policy_display(),
                "show_score_immediately": exam.show_score_immediately,
            },
            "count": len(rows),
            "results": rows,
        })


@extend_schema(tags=["判分 · 教师"])
class GradingExamListView(APIView):
    """有作答记录的考试列表——批改工作台顶部的考试选择器。"""

    permission_classes = [IsTeacher]

    @extend_schema(summary="可批改的考试列表")
    def get(self, request):
        queryset = Exam.objects.with_relations().filter(attempts__isnull=False).distinct()
        if not getattr(request.user, "is_admin", False):
            queryset = queryset.filter(created_by=request.user)

        rows = []
        for exam in queryset.order_by("-created_at")[:200]:
            attempts = exam.attempts.exclude(status=AttemptStatus.IN_PROGRESS)
            rows.append({
                "id": exam.id,
                "title": exam.title,
                "subject": exam.subject,
                "status": exam.status,
                "computed_status": exam.computed_status(),
                "total_score": float(exam.total_score or 0),
                "question_count": exam.question_count,
                "attempt_total": exam.attempts.count(),
                "submitted_total": attempts.count(),
                "graded_total": sum(1 for row in attempts if row.is_graded),
                "multiple_choice_policy_display": exam.get_multiple_choice_policy_display(),
            })
        return success(data={"count": len(rows), "results": rows})


# ======================================================================
# 成绩单
# ======================================================================
@extend_schema(tags=["成绩单"])
class AttemptReportView(APIView):
    """成绩单：教师看全量，学生看自己的（按公开规则脱敏）。

    学生会话访问时会**顺带触发一次客观题惰性判分**（幂等且不调大模型），
    这样「交卷后立刻看到客观题得分」不需要教师先点一次按钮。
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(summary="成绩单（逐题得分）")
    def get(self, request, attempt_id):
        attempt = _get_attempt(attempt_id, request.user)

        if not getattr(request.user, "is_teacher", False):
            try:
                GradingService.ensure_graded(attempt)
                attempt.refresh_from_db()
            except Exception as exc:  # noqa: BLE001 —— 判分失败不该让学生看不到答卷
                logger.warning("惰性判分失败（attempt=%s）：%s", attempt.id, exc)

        report = ScoreReportService.attempt_report(attempt, viewer=request.user)
        message = "成绩尚未公布，请联系教师" if not report["visible"] else "success"
        return success(data=report, message=message)


@extend_schema(tags=["成绩单"])
class ScoreboardView(APIView):
    """班级成绩单。"""

    permission_classes = [IsTeacher]

    @extend_schema(
        summary="班级成绩单（排名 / 分布 / 及格率）",
        parameters=[
            OpenApiParameter("exam_id", int, description="考试 id（必填）"),
            OpenApiParameter("keyword", str, description="按姓名 / 学号搜索"),
            OpenApiParameter("classroom_id", int, description="按班级过滤（可选）"),
        ],
    )
    def get(self, request):
        exam = _get_exam(request.query_params.get("exam_id"))
        student_ids = _classroom_student_ids(request.query_params.get("classroom_id"), request.user)
        data = ScoreReportService.scoreboard(
            exam, keyword=request.query_params.get("keyword", ""),
            student_ids=student_ids,
        )
        # 标记「能否查看该生学情」，让成绩单里的姓名只在有权限时才渲染成链接
        from apps.analytics.scoping import visible_student_ids

        visible = None
        if not getattr(request.user, "is_admin", False):
            visible = set(visible_student_ids(request.user))
        # 键名必须与 ScoreReportService.scoreboard() 的返回一致（是 results 不是 rows）。
        # 曾写成 data.get("rows") 取到 None → 一条都不打标 → 前端拿到 undefined，
        # 按「不等于 false 就当有权限」渲染成链接，班外学生照点不误、点进去 404。
        for row in data.get("results") or []:
            row["can_view_analytics"] = (
                True if visible is None else row.get("student_id") in visible
            )
        return success(data=data)


def _classroom_student_ids(classroom_id, user=None):
    """把 ``classroom_id`` 解析成学生 id 列表；没传返回 None。

    传了班级就**必须是自己能管理的班**（自己创建 / 任班主任 / 授课），否则 404 ——
    否则把 id 换个数字就能看别人班的成绩单。

    注意这里**不做**「默认只看自己班」的收窄：成绩单是批改工作台，教师需要看到
    这场考试的全部待批答卷（含跨班监考/统考），收窄会导致漏批。
    """
    if classroom_id is None or str(classroom_id).strip() == "":
        return None
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


@extend_schema(tags=["成绩单"])
class WrongBookView(APIView):
    """错题本。"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="错题本",
        parameters=[
            OpenApiParameter("student_id", int, description="教师查看指定学生（学生只能看自己）"),
            OpenApiParameter("subject", str, description="按学科过滤"),
            OpenApiParameter("limit", int, description="返回条数上限，默认 200"),
        ],
    )
    def get(self, request):
        from django.contrib.auth import get_user_model

        student_id = request.query_params.get("student_id")
        if getattr(request.user, "is_teacher", False):
            if not student_id:
                raise BusinessError("教师查看错题本需要指定 student_id")
            student = get_object_or_404(
                get_user_model(), pk=_as_int(student_id, "student_id"),
            )
        else:
            # 学生忽略传入的 student_id：**永远只能看自己的**，
            # 传别人的 id 不是报错而是被无视，避免拿它当「这个 id 是否存在」的探针
            student = request.user

        try:
            limit = max(1, min(int(request.query_params.get("limit") or 200), 500))
        except (TypeError, ValueError):
            limit = 200

        data = ScoreReportService.wrong_book(
            student, subject=request.query_params.get("subject", ""), limit=limit,
        )
        data["student"] = {
            "id": student.id,
            "name": getattr(student, "display_name", "") or "",
            "student_no": getattr(student, "student_no", "") or "",
        }
        return success(data=data)


@extend_schema(tags=["成绩单"])
class CalibrationView(APIView):
    """AI 批改校准统计。"""

    permission_classes = [IsTeacher]

    @extend_schema(summary="AI 批改校准统计")
    def get(self, request):
        return success(data=ScoreReportService.calibration(
            None if getattr(request.user, "is_admin", False) else request.user
        ))


# ======================================================================
# 复核记录
# ======================================================================
@extend_schema(tags=["判分 · 教师"])
class GradingFeedbackViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin,
                             viewsets.GenericViewSet):
    """复核记录（只读）：批改工作台展示「这道题我上次改成多少」。"""

    serializer_class = GradingFeedbackSerializer
    permission_classes = [IsTeacher]

    def get_queryset(self):
        queryset = GradingFeedback.objects.with_relations()
        attempt_id = self.request.query_params.get("attempt_id")
        answer_id = self.request.query_params.get("answer_id")
        if attempt_id:
            queryset = queryset.filter(attempt_id=_as_int(attempt_id, "attempt_id"))
        if answer_id:
            queryset = queryset.filter(answer_id=_as_int(answer_id, "answer_id"))
        return queryset


__all__ = [
    "AnswerReviewView",
    "AttemptFinalizeView",
    "AttemptGradeView",
    "AttemptReportView",
    "CalibrationView",
    "GradingExamListView",
    "GradingFeedbackViewSet",
    "GradingQueueView",
    "ScoreboardView",
    "WrongBookView",
]
