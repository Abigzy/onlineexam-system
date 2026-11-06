"""
个性化推荐模块的页面视图。

「每日练习」的页面对学生与教师是**同一个模板**：学生看到的是自己的推荐
（``student_id`` 取登录用户），教师从学情页点进来时看到的是某个学生的推荐。
页面只是「壳」——推荐清单、匹配度、推荐理由全部由前端调 RESTful 接口拿，
口径只留在服务端。
"""

import logging

from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.generic import TemplateView

from apps.common.mixins import StudentRequiredMixin

from .services import RecommendationService

logger = logging.getLogger("apps.recommend.pages")


@method_decorator(ensure_csrf_cookie, name="dispatch")
class DailyPracticePageView(StudentRequiredMixin, TemplateView):
    """学生：每日练习。

    ``?subject=`` 可以直接定位到某个学科（从学情页跳过来时用）。
    ``student_id`` 注入的是登录用户自己的 id——接口侧仍会独立校验一次
    （页面注入只是方便，不是信任来源）。
    """

    template_name = "recommend/practice.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user

        context["student_id"] = user.id
        context["student_name"] = getattr(user, "display_name", "") or user.username
        context["subject"] = (self.request.GET.get("subject") or "").strip()[:64]
        # 下拉先由服务端渲染一份：只列学生**已加入**的学科（学科班的学科），
        # 不依赖第一次接口返回（避免首屏只有「全部学科」的窗口期）。
        # 为空说明学生没加入任何学科班，模板据此渲染引导空态
        context["subject_options"] = RecommendationService.selectable_subjects(user)
        context["page_title"] = "每日练习"
        context["active_menu"] = "practice"
        context["is_self"] = True
        return context


__all__ = ["DailyPracticePageView"]
