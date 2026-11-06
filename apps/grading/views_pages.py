"""
判分与成绩模块的页面视图。

页面只做「壳」：渲染容器与初始上下文，数据全部由前端调 RESTful 接口。
批改工作台尤其如此——「判分 / 采纳建议 / 改分 / 出分」四个动作都是现成接口，
页面自己实现一遍就等于把「判分口径只有服务端一处」这条规则砸了。
"""

import logging

from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.generic import TemplateView

from apps.common.mixins import StudentRequiredMixin, TeacherPageMixin
from apps.exam.models import Exam

logger = logging.getLogger("apps.grading.pages")


@method_decorator(ensure_csrf_cookie, name="dispatch")
class GradingWorkbenchPageView(TeacherPageMixin, TemplateView):
    """教师：批改工作台。

    支持 ``?exam_id=`` 直接定位到某场考试（从考试监控页「去批改」跳过来时用）。
    """

    template_name = "grading/workbench.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        raw_exam_id = self.request.GET.get("exam_id") or ""
        context["exam_id"] = int(raw_exam_id) if raw_exam_id.isdigit() else None
        context["page_title"] = "批改工作台"
        context["active_menu"] = "grading"
        return context


@method_decorator(ensure_csrf_cookie, name="dispatch")
class ScoreboardPageView(TeacherPageMixin, TemplateView):
    """教师：班级成绩单。"""

    template_name = "grading/scoreboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        exam = get_object_or_404(Exam.objects.with_relations(), pk=self.kwargs.get("pk"))
        context["exam_id"] = exam.id
        context["exam_title"] = exam.title
        context["page_title"] = f"成绩单 · {exam.title}"
        context["active_menu"] = "grading"
        return context


@method_decorator(ensure_csrf_cookie, name="dispatch")
class WrongBookPageView(StudentRequiredMixin, TemplateView):
    """学生：错题本。"""

    template_name = "grading/wrong_book.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "我的错题本"
        context["active_menu"] = "wrong-book"
        return context
