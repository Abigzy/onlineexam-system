"""作弊行为检测接口。

路由总览（``/api/v1/`` 下，**全部教师专属**）：

    GET    /proctoring/exams/                    有行为数据的考试列表（页面选择器）
    GET    /proctoring/exams/{id}/report/        可疑考生列表 + 概览 + 异常检测
    GET    /proctoring/attempts/{id}/evidence/   一次作答的逐条证据链
    GET    /proctoring/rules/                    规则与阈值说明（「可解释」的依据）

权限边界：
  * 可疑名单与证据链是**监考数据**，只有教师 / 管理员能看（``IsTeacher``）；
    学生即使看自己的「嫌疑」，也应走教师告知的渠道，不在这里开接口。
  * 资源不存在返回 **404**，与模块五 / 六 / 七一致。
"""

import logging

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework.views import APIView

from apps.common.permissions import IsTeacher
from apps.common.response import success
from apps.exam.models import Exam, ExamAttempt

from .serializers import ProctoringReportQuerySerializer
from .services import ProctoringReportService

logger = logging.getLogger("apps.proctoring.views")


def _get_exam(exam_id: int) -> Exam:
    return get_object_or_404(Exam.objects.select_related("paper"), pk=exam_id)


def _get_attempt(attempt_id: int) -> ExamAttempt:
    return get_object_or_404(
        ExamAttempt.objects.select_related("exam", "student"), pk=attempt_id,
    )


@extend_schema(tags=["作弊行为检测"])
class ProctoringExamListView(APIView):
    """有行为埋点的考试列表。"""

    permission_classes = [IsTeacher]

    @extend_schema(summary="有行为数据的考试列表")
    def get(self, request):
        rows = ProctoringReportService.exam_options(request.user)
        return success(data={"count": len(rows), "results": rows})


@extend_schema(tags=["作弊行为检测"])
class ProctoringExamReportView(APIView):
    """某场考试的作弊检测报告：可疑考生列表 + 概览 + 异常检测。"""

    permission_classes = [IsTeacher]

    @extend_schema(summary="班级作弊检测报告（可疑考生列表）")
    def get(self, request, exam_id):
        exam = _get_exam(exam_id)
        serializer = ProctoringReportQuerySerializer(data=request.query_params or {})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        report = ProctoringReportService.exam_report(
            exam,
            only_suspicious=bool(data.get("only_suspicious")),
            top_n=int(data.get("top_n") or 0),
        )
        return success(data=report)


@extend_schema(tags=["作弊行为检测"])
class ProctoringAttemptEvidenceView(APIView):
    """一次作答的逐条证据链。"""

    permission_classes = [IsTeacher]

    @extend_schema(summary="作答证据链（逐条行为埋点 + 命中规则 + 异常分）")
    def get(self, request, attempt_id):
        attempt = _get_attempt(attempt_id)
        return success(data=ProctoringReportService.attempt_evidence(attempt))


@extend_schema(tags=["作弊行为检测"])
class ProctoringRulesView(APIView):
    """规则目录与阈值说明。"""

    permission_classes = [IsTeacher]

    @extend_schema(summary="作弊检测规则与阈值说明")
    def get(self, request):
        return success(data=ProctoringReportService.rules_catalog())


__all__ = [
    "ProctoringAttemptEvidenceView",
    "ProctoringExamListView",
    "ProctoringExamReportView",
    "ProctoringRulesView",
]
