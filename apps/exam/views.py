"""
在线考试接口。

路由总览（`/api/v1/` 下）：

    考试
    GET    /exams/                          列表（教师看自己/全部的；学生看「我的考试」）
    POST   /exams/                          新建考试
    GET    /exams/{id}/                     详情（教师=完整配置；学生=概要，不含试卷内容）
    PUT    /exams/{id}/                     全量修改
    PATCH  /exams/{id}/                     局部修改
    DELETE /exams/{id}/                     删除（已有作答记录的不允许删，改归档）
    POST   /exams/{id}/status/              发布 / 开始 / 结束 / 归档
    POST   /exams/{id}/start/               开始或继续作答（学生）→ 下发试卷（**剔除答案**）
    GET    /exams/{id}/overview/            单场考试统计（应考/进行中/已交/行为预警）
    GET    /exams/{id}/attempts/            作答明细表（教师看板）
    GET    /exams/statistics/               全部考试总览
    GET    /exams/candidates/               可选学生名单（建考试时选人）

    作答
    GET    /attempts/                       作答列表（学生=自己的；教师=全部）
    GET    /attempts/{id}/                  作答概要
    GET    /attempts/{id}/paper/            卷面 + 已存答案（断网续考 / 刷新恢复）
    GET    /attempts/{id}/answers/          逐题作答明细（教师批改 / 本人查看）
    POST   /attempts/{id}/answers/          批量暂存
    POST   /attempts/{id}/save/             单题暂存
    POST   /attempts/{id}/heartbeat/        心跳（返回服务端权威剩余时间）
    POST   /attempts/{id}/behavior/         行为上报（切屏 / 粘贴 / 退出全屏）
    POST   /attempts/{id}/submit/           交卷
    GET    /attempts/{id}/result/           结果（判分后才给分）

权限：考试配置类操作全部要求教师；作答类操作只允许本人（学生）。
学生永远拿不到答案——下发走 ``ExamAttemptService`` 的唯一出口。
"""

import logging

from django.contrib.auth import get_user_model
from django.db import models
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated

from apps.common.constants import AttemptStatus, ExamStatus, Role
from apps.common.exceptions import BusinessError
from apps.common.pagination import StandardPagination
from apps.common.permissions import IsStudent, IsTeacher
from apps.common.response import created, success
from apps.common.scoping import teacher_subject_scope

from .filters import ExamAttemptFilter, ExamFilter
from .models import Exam, ExamAttempt
from .serializers import (
    AnswerSerializer,
    AnswerStudentSerializer,
    BehaviorReportSerializer,
    ExamAttemptSerializer,
    ExamAttemptStudentSerializer,
    ExamDetailSerializer,
    ExamListSerializer,
    ExamStatusChangeSerializer,
    ExamStudentSerializer,
    ExamWriteSerializer,
    HeartbeatSerializer,
    SaveAnswerSerializer,
    SaveAnswersSerializer,
    StartExamSerializer,
    SubmitSerializer,
)
from .services import ExamAnalysisService, ExamAttemptService, ExamService

logger = logging.getLogger("apps.exam.views")


def _client_ip(request) -> str:
    """取真实客户端 IP。Nginx 反代后 REMOTE_ADDR 是网关地址，优先读 X-Forwarded-For。"""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


@extend_schema(tags=["考试"])
class ExamViewSet(viewsets.ModelViewSet):
    """考试的增删改查与开考。

    同一个资源、两种视角：教师看配置，学生看「能不能考、考到哪了」。
    用 ``get_permissions`` / ``get_queryset`` / ``get_serializer_class`` 三处分流，
    而不是拆成两个 ViewSet——否则路由上会出现两个 `/exams/`，前端要记两套。
    """

    queryset = Exam.objects.all()
    serializer_class = ExamListSerializer
    pagination_class = StandardPagination
    filterset_class = ExamFilter
    search_fields = ["title", "description", "subject"]
    ordering_fields = ["created_at", "updated_at", "start_time", "end_time", "published_at"]
    ordering = ["-created_at"]

    # ------------------------------------------------------------------
    # 分流
    # ------------------------------------------------------------------
    def get_permissions(self):
        if self.action == "start":
            return [IsStudent()]
        if self.action in ("list", "retrieve"):
            # 教师看全部、学生看自己范围内的；具体看到什么由 get_queryset /
            # get_serializer_class 决定，不在这里挡人——挡了学生就看不到「我的考试」了
            return [IsAuthenticated()]
        return [IsTeacher()]

    def get_queryset(self):
        user = self.request.user
        queryset = Exam.objects.with_relations()

        if getattr(user, "is_teacher", False):
            queryset = teacher_subject_scope(user, queryset)
            if self.action == "list" and not self.request.query_params.get("status"):
                queryset = queryset.active()
            return queryset.with_counts()

        # 学生：只能看到自己范围内的、且已发布的考试
        return queryset.for_student(user)

    def get_serializer_class(self):
        user = self.request.user
        teacher = getattr(user, "is_teacher", False)

        if self.action == "list":
            return ExamListSerializer if teacher else ExamStudentSerializer
        if self.action == "retrieve":
            return ExamDetailSerializer if teacher else ExamStudentSerializer
        if self.action in ("create", "update", "partial_update"):
            return ExamWriteSerializer
        return ExamDetailSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["student"] = self.request.user
        if getattr(self.request.user, "is_teacher", False):
            context["all_student_count"] = get_user_model().objects.filter(
                role=Role.STUDENT, is_active=True
            ).count()
        return context

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    @extend_schema(summary="新建考试", request=ExamWriteSerializer, responses=ExamDetailSerializer)
    def create(self, request, *args, **kwargs):
        serializer = ExamWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        exam = ExamService.create(data=serializer.to_service_payload(), user=request.user)
        return created(
            data=ExamDetailSerializer(self._reload(exam), context=self.get_serializer_context()).data,
            message=f"考试《{exam.title}》已创建（草稿）",
        )

    @extend_schema(summary="修改考试", request=ExamWriteSerializer, responses=ExamDetailSerializer)
    def update(self, request, *args, **kwargs):
        exam = self.get_object()
        serializer = ExamWriteSerializer(data=request.data, partial=kwargs.get("partial", False))
        serializer.is_valid(raise_exception=True)
        exam = ExamService.update(exam, data=serializer.to_service_payload())
        return success(
            data=ExamDetailSerializer(self._reload(exam), context=self.get_serializer_context()).data,
            message="已保存",
        )

    @extend_schema(summary="删除考试")
    def destroy(self, request, *args, **kwargs):
        exam = self.get_object()
        title = exam.title
        ExamService.delete(exam)
        return success(message=f"考试《{title}》已删除")

    def _reload(self, exam: Exam) -> Exam:
        return Exam.objects.with_relations().with_counts().get(pk=exam.pk)

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    @extend_schema(summary="变更考试状态（发布 / 开始 / 结束 / 归档）",
                   request=ExamStatusChangeSerializer)
    @action(detail=True, methods=["post"])
    def status(self, request, pk=None):
        exam = self.get_object()
        serializer = ExamStatusChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        exam = ExamService.change_status(exam, serializer.validated_data["status"])
        exam = self._reload(exam)
        return success(
            data=ExamDetailSerializer(exam, context=self.get_serializer_context()).data,
            message=f"考试已切换到「{exam.get_status_display()}」",
        )

    # ------------------------------------------------------------------
    # 开考（学生）
    # ------------------------------------------------------------------
    @extend_schema(summary="开始 / 继续作答（学生）", request=StartExamSerializer)
    @action(detail=True, methods=["post"])
    def start(self, request, pk=None):
        exam = self.get_object()
        serializer = StartExamSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)

        result = ExamAttemptService.start(
            exam=exam, student=request.user,
            ip=_client_ip(request), user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )
        attempt = result["attempt"]
        payload = ExamAttemptService.get_state(attempt)

        message = "已恢复到上次的作答进度" if result["resumed"] else "考试已开始，祝你好运"
        return success(data=payload, message=message)

    # ------------------------------------------------------------------
    # 教师看板
    # ------------------------------------------------------------------
    @extend_schema(summary="单场考试统计")
    @action(detail=True, methods=["get"])
    def overview(self, request, pk=None):
        exam = self.get_object()
        return success(data=ExamAnalysisService.overview(exam))

    @extend_schema(
        summary="作答明细表（教师看板）",
        parameters=[
            OpenApiParameter("status", description="按状态筛选，如 in_progress"),
            OpenApiParameter("keyword", description="按姓名 / 学号 / 用户名搜索"),
        ],
    )
    @action(detail=True, methods=["get"])
    def attempts(self, request, pk=None):
        exam = self.get_object()
        rows = ExamAnalysisService.attempt_table(
            exam,
            status=request.query_params.get("status", ""),
            keyword=request.query_params.get("keyword", ""),
        )
        # 标记「当前用户能否查看该生学情」。跨班考试时前端据此决定是否渲染成链接，
        # 避免出现一堆点进去 404 的名字。惰性 import：exam 不能反向依赖 analytics 顶层模块。
        from apps.analytics.scoping import visible_student_ids

        visible = None
        if not getattr(request.user, "is_admin", False):
            visible = set(visible_student_ids(request.user))
        for row in rows:
            row["can_view_analytics"] = (
                True if visible is None else row.get("student_id") in visible
            )
        return success(data={
            "overview": ExamAnalysisService.overview(exam),
            "results": rows,
            "count": len(rows),
        })

    @extend_schema(summary="全部考试总览")
    @action(detail=False, methods=["get"])
    def statistics(self, request):
        return success(data=ExamAnalysisService.global_statistics(request.user))

    @extend_schema(
        summary="可选学生名单",
        parameters=[OpenApiParameter("keyword", description="按姓名 / 学号搜索")],
    )
    @action(detail=False, methods=["get"])
    def candidates(self, request):
        """建考试时挑「指定考生」用。按班级分组返回，前端「选一个班 = 选该班所有人」。

        范围：教师只看自己可管理的班（自己创建 / 任班主任 / 授课）；
        管理员看全部，且不在任何班里的学生单独归「未分班」一组。
        """
        from apps.classroom.models import Classroom

        keyword = request.query_params.get("keyword", "")

        def _filter(qs):
            if not keyword:
                return qs
            return qs.filter(
                models.Q(username__icontains=keyword)
                | models.Q(real_name__icontains=keyword)
                | models.Q(student_no__icontains=keyword)
            )

        if getattr(request.user, "is_admin", False):
            classrooms = Classroom.objects.all()
            all_students = get_user_model().objects.filter(role=Role.STUDENT, is_active=True)
        else:
            classrooms = Classroom.objects.manageable_by(request.user)
            all_students = None

        def _rows(members):
            return [
                {"id": s.id, "name": s.display_name,
                 "student_no": getattr(s, "student_no", "") or ""}
                for s in members.order_by("student_no", "username")
            ]

        groups, seen = [], set()
        for cls in classrooms.order_by("subject", "name"):
            members = _filter(cls.students.filter(role=Role.STUDENT, is_active=True))
            if not members.exists():
                continue
            rows = _rows(members)
            seen.update(r["id"] for r in rows)
            groups.append({"id": cls.id, "name": cls.name, "subject": cls.subject or "", "students": rows})

        if all_students is not None:
            unassigned = _filter(all_students.exclude(pk__in=seen))
            if unassigned.exists():
                groups.append({"id": None, "name": "未分班", "subject": "", "students": _rows(unassigned[:500])})

        return success(data={"classrooms": groups})


@extend_schema(tags=["考试 · 作答"])
class AttemptViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """作答记录：学生自己的作答全流程 + 教师的查阅。"""

    serializer_class = ExamAttemptSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = StandardPagination
    filterset_class = ExamAttemptFilter
    ordering_fields = ["started_at", "submitted_at", "score"]
    ordering = ["-started_at"]

    def get_permissions(self):
        # 只有「会产生作答数据」的动作限学生；读类的动作教师也要能看（批改、复盘）。
        # 注意 ``answers`` 是 GET/POST 同路径：读放给教师，写在视图里再卡一次身份。
        if self.action in ("paper", "save", "heartbeat", "behavior", "submit"):
            return [IsStudent()]
        return [IsAuthenticated()]

    def get_queryset(self):
        queryset = ExamAttempt.objects.with_relations()
        if getattr(self.request.user, "is_teacher", False):
            return queryset
        return queryset.filter(student=self.request.user)

    def get_serializer_class(self):
        if not getattr(self.request.user, "is_teacher", False):
            # answers / paper 也要在这里列出：它们响应体里都嵌了一个 attempt 对象，
            # 漏掉就会用教师版序列化器，把 score 之类的字段直接带给学生。
            if self.action in ("list", "retrieve", "result", "answers", "paper"):
                return ExamAttemptStudentSerializer
        return ExamAttemptSerializer

    # ------------------------------------------------------------------
    # 续考
    # ------------------------------------------------------------------
    @extend_schema(summary="获取卷面与已存答案（断网续考 / 刷新恢复）")
    @action(detail=True, methods=["get"])
    def paper(self, request, pk=None):
        attempt = self.get_object()
        payload = ExamAttemptService.get_state(attempt)
        if payload.get("finished"):
            return success(data=payload, message="本次作答已结束")
        return success(data=payload)

    # ------------------------------------------------------------------
    # 作答明细
    # ------------------------------------------------------------------
    @extend_schema(summary="逐题作答明细（GET 查看 / POST 批量暂存）")
    @action(detail=True, methods=["get", "post"], url_path="answers")
    def answers(self, request, pk=None):
        attempt = self.get_object()

        if request.method == "GET":
            rows = attempt.answers.select_related(
                "paper_question__question", "paper_question__section",
                # 逐题对错要按 attempt.exam 的可见性配置决定给不给，预先取出来避免 N+1
                "attempt__exam",
            ).order_by("order", "id")
            # 按角色分流：教师批改需要参考答案，学生**一律不给**。
            # 学生要看答案是交卷后走 /result/（受「立即公布成绩」门禁），
            # 不能从考试进行中的明细接口顺手取走。
            is_teacher = getattr(request.user, "is_teacher", False)
            serializer_cls = AnswerSerializer if is_teacher else AnswerStudentSerializer
            return success(data={
                "attempt": self.get_serializer(attempt).data,
                "count": rows.count(),
                "results": serializer_cls(rows, many=True).data,
            })

        # POST：学生批量暂存
        if not getattr(request.user, "is_student", False):
            raise BusinessError("只有考生本人可以提交答案", code=10431, status_code=403)
        serializer = SaveAnswersSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = ExamAttemptService.save_answers(attempt, serializer.validated_data["items"])
        return success(data=result, message=f"已保存 {result['saved']} 道题")

    @extend_schema(summary="单题暂存", request=SaveAnswerSerializer)
    @action(detail=True, methods=["post"])
    def save(self, request, pk=None):
        attempt = self.get_object()
        serializer = SaveAnswerSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = ExamAttemptService.save_answer(attempt, **serializer.validated_data)
        return success(data=result, message="已保存")

    # ------------------------------------------------------------------
    # 心跳与行为
    # ------------------------------------------------------------------
    @extend_schema(summary="心跳（返回服务端权威剩余时间）", request=HeartbeatSerializer)
    @action(detail=True, methods=["post"])
    def heartbeat(self, request, pk=None):
        attempt = self.get_object()
        serializer = HeartbeatSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        result = ExamAttemptService.heartbeat(
            attempt, client_remaining=serializer.validated_data.get("client_remaining"),
        )
        return success(data=result)

    @extend_schema(summary="行为上报（切屏 / 粘贴 / 退出全屏）", request=BehaviorReportSerializer)
    @action(detail=True, methods=["post"])
    def behavior(self, request, pk=None):
        attempt = self.get_object()
        serializer = BehaviorReportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = ExamAttemptService.record_behavior(attempt, serializer.validated_data["events"])

        message = "已记录"
        if result["auto_submitted"]:
            message = "切屏次数已超限，系统已强制交卷"
        elif result["limit_exceeded"]:
            message = f"切屏次数已达 {result['switch_count']} 次，超过本场考试上限"
        return success(data=result, message=message)

    # ------------------------------------------------------------------
    # 交卷与结果
    # ------------------------------------------------------------------
    @extend_schema(summary="交卷", request=SubmitSerializer)
    @action(detail=True, methods=["post"])
    def submit(self, request, pk=None):
        attempt = self.get_object()
        if not getattr(request.user, "is_student", False):
            raise BusinessError("只有考生本人可以交卷", code=10431, status_code=403)

        serializer = SubmitSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)

        # 交卷前把前端最后一波作答落盘，避免「点了交卷但最后一题没存上」
        items = serializer.validated_data.get("items") or []
        if items:
            try:
                ExamAttemptService.save_answers(attempt, items)
            except BusinessError as exc:
                logger.warning("交卷前落盘失败（不影响交卷）：%s", exc)

        attempt = ExamAttemptService.submit(
            attempt, reason=serializer.validated_data["reason"],
        )
        data = ExamAttemptStudentSerializer(attempt).data
        message = "时间到，已自动交卷" if attempt.is_timeout else "交卷成功"
        return success(data=data, message=message)

    @extend_schema(summary="查看结果")
    @action(detail=True, methods=["get"])
    def result(self, request, pk=None):
        attempt = self.get_object()
        data = ExamAttemptStudentSerializer(attempt).data

        # 成绩是否公开由考试配置决定；未公开时前端只显示「已交卷，等待阅卷」
        detail = ExamAttemptService._attempt_brief(attempt)
        detail["show_score"] = data.get("show_score")
        if data.get("show_score"):
            rows = attempt.answers.select_related("paper_question__question").order_by("order", "id")
            detail["answers"] = AnswerSerializer(rows, many=True).data
        return success(data=detail)
