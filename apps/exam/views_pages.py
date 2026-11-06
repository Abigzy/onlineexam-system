"""
考试模块的页面视图。

页面只做「壳」：渲染出容器与初始上下文，数据全部由前端调 RESTful 接口拿。
答题页尤其如此——计时、暂存、交卷、行为上报全都是现成接口，
页面自己实现一遍就等于把「服务端是唯一计时权威」这条规则砸了。
"""

import logging

from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.generic import TemplateView

from apps.common.constants import ExamStatus
from apps.common.exceptions import BusinessError
from apps.common.mixins import StudentRequiredMixin, TeacherPageMixin
from apps.questions.models import KnowledgePoint

from .constants import AUTOSAVE_INTERVAL_SECONDS, HEARTBEAT_INTERVAL_SECONDS
from .models import Exam

logger = logging.getLogger("apps.exam.pages")


def _subjects() -> list:
    return list(
        KnowledgePoint.objects.exclude(subject="")
        .values_list("subject", flat=True).distinct().order_by("subject")
    )


@method_decorator(ensure_csrf_cookie, name="dispatch")
class ExamListPageView(TeacherPageMixin, TemplateView):
    """教师：考试管理（发布、监控、归档）。"""

    template_name = "exam/list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "考试管理"
        context["active_menu"] = "exams"
        context["subjects"] = _subjects()
        context["status_choices"] = [
            {"value": code, "label": label} for code, label in ExamStatus.choices
        ]
        return context


@method_decorator(ensure_csrf_cookie, name="dispatch")
class ExamMonitorPageView(TeacherPageMixin, TemplateView):
    """教师：单场考试监控（作答进度 + 行为预警）。"""

    template_name = "exam/monitor.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        exam_id = self.kwargs.get("pk")
        exam = get_object_or_404(Exam.objects.with_relations(), pk=exam_id)
        context["exam_id"] = exam.id
        context["exam_title"] = exam.title
        context["page_title"] = f"考试监控 · {exam.title}"
        context["active_menu"] = "exams"
        return context


@method_decorator(ensure_csrf_cookie, name="dispatch")
class MyExamListPageView(StudentRequiredMixin, TemplateView):
    """学生：我的考试。"""

    template_name = "exam/index.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "我的考试"
        context["active_menu"] = "my-exams"
        return context


@method_decorator(ensure_csrf_cookie, name="dispatch")
class ExamTakePageView(StudentRequiredMixin, TemplateView):
    """学生：答题页。

    进来先做一次「能不能考」的检查，不能考就直接给出原因，别让学生看到一片空白
    再点半天按钮才发现开不了。
    """

    template_name = "exam/take.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        exam_id = self.kwargs.get("pk")
        exam = get_object_or_404(Exam.objects.with_relations(), pk=exam_id)

        student = self.request.user
        if not exam.open_to(student):
            raise BusinessError("你不在本场考试的参考范围内", code=10431, status_code=403)

        context["exam_id"] = exam.id
        context["exam_title"] = exam.title
        context["exam_description"] = exam.description
        context["page_title"] = f"答题 · {exam.title}"
        context["active_menu"] = "my-exams"
        context["autosave_seconds"] = AUTOSAVE_INTERVAL_SECONDS
        context["heartbeat_seconds"] = HEARTBEAT_INTERVAL_SECONDS
        return context


@method_decorator(ensure_csrf_cookie, name="dispatch")
class ExamResultPageView(StudentRequiredMixin, TemplateView):
    """学生：交卷结果 / 成绩单。"""

    template_name = "exam/result.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        exam_id = self.kwargs.get("pk")
        exam = get_object_or_404(Exam.objects.with_relations(), pk=exam_id)

        attempt = exam.my_attempt(self.request.user)
        context["exam_id"] = exam.id
        context["exam_title"] = exam.title
        context["attempt_id"] = attempt.id if attempt else None
        context["page_title"] = f"考试结果 · {exam.title}"
        context["active_menu"] = "my-exams"
        return context
