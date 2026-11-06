"""
学情分析模块的页面视图。

页面只是「壳」：渲染容器 + 初始上下文，数据全部由前端调 RESTful 接口。
这一点在学情模块尤其重要——掌握度、分布、区分度的口径必须只留在服务端，
页面自己再算一遍就会出现「仪表盘说 62%，报告里说 58%」这种没法解释的差异。
"""

import logging

from django.contrib.auth import get_user_model
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.generic import TemplateView

from apps.common.mixins import StudentRequiredMixin, TeacherPageMixin

from .scoping import can_view_student

logger = logging.getLogger("apps.analytics.pages")


@method_decorator(ensure_csrf_cookie, name="dispatch")
class AnalyticsDashboardPageView(TeacherPageMixin, TemplateView):
    """教师：班级学情仪表盘（ECharts 四图一屏）。

    支持 ``?exam_id=`` 直接定位到某场考试（从成绩单页「看学情」跳过来时用）。
    """

    template_name = "analytics/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        raw_exam_id = self.request.GET.get("exam_id") or ""
        context["exam_id"] = int(raw_exam_id) if raw_exam_id.isdigit() else None
        context["page_title"] = "学情分析"
        context["active_menu"] = "analytics"
        return context


@method_decorator(ensure_csrf_cookie, name="dispatch")
class StudentAnalyticsPageView(TeacherPageMixin, TemplateView):
    """教师：查看某个学生的学情详情。"""

    template_name = "analytics/student.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        student = get_object_or_404(get_user_model(), pk=self.kwargs.get("pk"))
        # 页面与接口用同一套口径，否则会出现「页面能打开、数据全是 404」的割裂体验
        if not can_view_student(self.request.user, student):
            raise Http404("学生不存在")
        context["student_id"] = student.id
        context["student_name"] = getattr(student, "display_name", "") or student.username
        context["page_title"] = f"学情 · {context['student_name']}"
        context["active_menu"] = "analytics"
        return context


@method_decorator(ensure_csrf_cookie, name="dispatch")
class MyAnalyticsPageView(StudentRequiredMixin, TemplateView):
    """学生：我的学情。

    直接把 ``student_id`` 注入页面——学生调接口时用的就是自己的 id，
    而接口侧的 ``_get_student`` 仍然会独立校验一次（页面注入只是方便，
    不是信任来源）。
    """

    template_name = "analytics/student.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["student_id"] = self.request.user.id
        context["student_name"] = (getattr(self.request.user, "display_name", "")
                                   or self.request.user.username)
        context["page_title"] = "我的学情"
        context["active_menu"] = "my-analytics"
        context["is_self"] = True
        return context


__all__ = [
    "AnalyticsDashboardPageView",
    "MyAnalyticsPageView",
    "StudentAnalyticsPageView",
]
