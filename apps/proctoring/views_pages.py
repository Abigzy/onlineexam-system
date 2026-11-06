"""
作弊行为检测模块的页面视图（教师侧）。

页面只是「壳」：可疑名单、证据链、异常分全部由前端调 RESTful 接口拿——
判定口径只留在服务端，页面自己再算一遍就会出现「看板说高度可疑、详情页说正常」。
"""

import logging

from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.generic import TemplateView

from apps.common.mixins import TeacherPageMixin

logger = logging.getLogger("apps.proctoring.pages")


@method_decorator(ensure_csrf_cookie, name="dispatch")
class ProctoringDashboardPageView(TeacherPageMixin, TemplateView):
    """教师：作弊检测看板（可疑考生列表）。

    支持 ``?exam_id=`` 直接定位到某场考试（从考试监控页跳过来时用）。
    """

    template_name = "proctoring/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        raw_exam_id = self.request.GET.get("exam_id") or ""
        context["exam_id"] = int(raw_exam_id) if raw_exam_id.isdigit() else None
        context["page_title"] = "作弊行为检测"
        context["active_menu"] = "proctoring"
        return context


@method_decorator(ensure_csrf_cookie, name="dispatch")
class ProctoringEvidencePageView(TeacherPageMixin, TemplateView):
    """教师：一次作答的证据链详情。"""

    template_name = "proctoring/evidence.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        attempt_id = self.kwargs.get("pk")
        context["attempt_id"] = int(attempt_id) if attempt_id else 0
        context["page_title"] = "作答证据链"
        context["active_menu"] = "proctoring"
        return context


__all__ = ["ProctoringDashboardPageView", "ProctoringEvidencePageView"]
