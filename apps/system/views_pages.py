"""管理控制台的页面视图（服务端渲染）。

四个页面共用 ``AdminConsoleMixin``：未登录跳登录页，非管理员 302 首页 + 提示。

渲染策略（PRD §3.1 / AC-36）：
  * **服务端渲染**——列表、组织、配置三页的数据在视图里算好直接塞进模板，
    首屏只有 1 个 HTML 请求，不额外发接口请求；
  * 数据口径统一委托 ``SystemOverviewService``，页面与 ``/api/v1/system/overview/``
    不会出现「两个地方数出两个数」的问题；
  * 危险动作（重置密码 / 停用）才走 JS 调既有 API，见 ``static/js/manage.js``。
"""

import logging

from django.views.generic import TemplateView

from apps.common.mixins import AdminConsoleMixin

from .services import SystemOverviewService

logger = logging.getLogger("apps.system.pages")


class ManageDashboardPageView(AdminConsoleMixin, TemplateView):
    """``GET /manage/`` 数据总览（管理员登录后的落地页，AC-08 / AC-09 / AC-10）。"""

    template_name = "system/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        overview = SystemOverviewService.overview()
        context["page_title"] = "数据总览"
        context["active_menu"] = "manage-dashboard"
        context["overview"] = overview
        # 两张 CSS 图的量程：模板用 {% widthratio %} 换算百分比，峰值至少为 1 避免除零
        context["ai_trend_peak"] = max([p["calls"] for p in overview["ai"]["trend"]] + [1])
        context["exam_status_peak"] = max(
            [row["total"] for row in overview["teaching"]["exams_by_status"]] + [1]
        )
        return context


class ManageOrgPageView(AdminConsoleMixin, TemplateView):
    """``GET /manage/org/`` 组织与基础数据（班级 / 学科分布 / 教师学科绑定）。"""

    template_name = "system/org.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "组织与基础数据"
        context["active_menu"] = "manage-org"
        context["org"] = SystemOverviewService.org_snapshot()
        return context


class ManageSystemPageView(AdminConsoleMixin, TemplateView):
    """``GET /manage/system/`` 系统配置（Provider 诊断 + 运行环境 + 开发者工具）。"""

    template_name = "system/config.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "系统配置"
        context["active_menu"] = "manage-system"
        context["provider"] = SystemOverviewService.provider_status()
        context["ai"] = SystemOverviewService.ai_usage()
        context["system"] = SystemOverviewService.system_info()
        return context


__all__ = [
    "ManageDashboardPageView",
    "ManageOrgPageView",
    "ManageSystemPageView",
]
