"""班级模块的页面视图（服务端渲染）。

页面只是「壳」：班级列表、成员、编辑全部由前端调 RESTful 接口拿——
可见范围 / 可管理范围的口径只留在服务端（``ClassroomQuerySet.visible_to`` /
``manageable_by``），页面自己再判断一遍就会「老师能管、管理员看得到」各说各话。

一个模板服务两种角色：
  * 教师 / 管理员 —— 建班、编辑、成员增删（管理视角）；
  * 学生 —— 只读看自己所在班级与同学。
"""

import logging

from django.db.models import Count
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.generic import TemplateView

from apps.common.mixins import ClassroomAccessMixin

from .constants import ClassType
from .models import Classroom

logger = logging.getLogger("apps.classroom.pages")


@method_decorator(ensure_csrf_cookie, name="dispatch")
class ClassroomListPageView(ClassroomAccessMixin, TemplateView):
    """班级列表（教师 / 学生二态）。"""

    template_name = "classroom/list.html"
    login_url = "/login/"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "班级管理"
        context["active_menu"] = "classroom"
        return context


@method_decorator(ensure_csrf_cookie, name="dispatch")
class ClassroomDetailPageView(ClassroomAccessMixin, TemplateView):
    """班级详情（成员管理 / 编辑）。"""

    template_name = "classroom/detail.html"
    login_url = "/login/"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["classroom_id"] = int(self.kwargs.get("pk") or 0)
        context["page_title"] = "班级详情"
        context["active_menu"] = "classroom"
        context["admin_classrooms"] = self._admin_classrooms()
        return context

    def _admin_classrooms(self) -> list:
        """「整班导入」下拉的数据源：启用中的行政班（id / 名称 / 人数）。

        人数用 ``annotate`` 聚合：模型上的 ``student_count`` 是 property，
        用同名注解会被 property 遮蔽并退回 N+1，所以这里换别名 ``member_total``。
        """
        if not getattr(self.request.user, "is_teacher", False):
            return []
        rows = (
            Classroom.objects.filter(class_type=ClassType.ADMIN, is_active=True)
            .annotate(member_total=Count("students"))
            .order_by("name")
            .values("id", "name", "member_total")
        )
        return [
            {"id": row["id"], "name": row["name"], "student_count": row["member_total"]}
            for row in rows
        ]


__all__ = ["ClassroomDetailPageView", "ClassroomListPageView"]
