"""页面视图的通用混入。

三层守卫（由外到内）：
  * L1 登录：``LoginRequiredMixin``（Django 原生）——未登录跳 ``/login/?next=``；
  * L2 角色：``RoleRequiredMixin`` 及其子类——默认拒绝形态是 **403**（``PermissionDenied``）；
  * L2' 管理端角色：``AdminConsoleMixin``——把拒绝形态换成「302 首页 + 消息提示」，
        因为管理端页面不能被 403 白页挡住（PRD §2.4：拒绝形态不用 403 白页）；
  * L3 账号状态：``InactiveSessionGuardMiddleware``（见 ``apps/common/middleware.py``）。

**关键纪律**：``RoleRequiredMixin`` 的默认拒绝形态必须是 ``PermissionDenied``，
不能为了管理端页面改成重定向——``apps/proctoring/tests/test_api.py`` 断言
学生访问 ``/proctoring/`` 返回 **403**。差异化拒绝一律通过覆写
``handle_no_role()`` 钩子实现，不动基类默认行为。
"""

from django.contrib import messages
from django.contrib.auth.mixins import AccessMixin, LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect


class RoleRequiredMixin(AccessMixin):
    """要求登录且角色符合要求。

    用法：
        class SomePageView(RoleRequiredMixin, TemplateView):
            required_roles = ("teacher", "admin")

    拒绝分两步：
        1. 未登录 → ``handle_no_permission()``（Django 原生，跳登录页）；
        2. 已登录但角色不符 → ``handle_no_role()``（钩子，默认 403）。
    """

    required_roles = ()

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if self.required_roles and not self._has_role(request.user):
            return self.handle_no_role()
        return super().dispatch(request, *args, **kwargs)

    def handle_no_role(self):
        """角色不符时的拒绝动作。

        默认与改造前完全等价（抛 403），子类可覆写为「重定向 + 提示」。
        """
        raise PermissionDenied("当前账号没有访问该页面的权限")

    def _has_role(self, user) -> bool:
        if getattr(user, "is_superuser", False):
            return True
        return getattr(user, "role", None) in self.required_roles


class TeacherRequiredMixin(RoleRequiredMixin):
    """教师或管理员。"""

    required_roles = ("teacher", "admin")


class StudentRequiredMixin(RoleRequiredMixin):
    """仅学生。"""

    required_roles = ("student",)


class AdminRequiredMixin(RoleRequiredMixin):
    """仅管理员（拒绝时抛 403）。"""

    required_roles = ("admin",)


class AdminConsoleMixin(AdminRequiredMixin):
    """管理端（``/manage/*``）页面守卫。

    未登录 → ``/login/?next=``（沿用 ``AccessMixin`` 默认行为）；
    已登录但非管理员 → **302 首页 + 错误提示**，不出现 403 白页（AC-06 / AC-07）。
    """

    def handle_no_role(self):
        messages.error(self.request, "该区域仅管理员可访问")
        return redirect("home")


class TeacherPageMixin(TeacherRequiredMixin):
    """教师业务页：管理员进入即视为「代管」。

    只补两个上下文标记，权限判定与 ``TeacherRequiredMixin`` 等价：
      * ``is_teacher_page``：``base.html`` 据此展开「教学管理（代管）」分组并高亮；
      * ``is_delegated``：``base.html`` 据此渲染「你正在以管理员身份代管」提示条。
    教师自己访问时 ``is_delegated=False``，视觉零变化（AC-04）。
    """

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["is_teacher_page"] = True
        context["is_delegated"] = bool(getattr(self.request.user, "is_admin", False))
        return context


class ClassroomAccessMixin(RoleRequiredMixin):
    """班级页：教师/管理员可管理，学生只读自己所在班级。

    ``templates/classroom/list.html`` 用 ``{% if user.is_teacher %}`` 分流两种视角，
    所以这里不能收成「仅教师」（学生要能看「我的班级」），也不能只校验登录。
    「能看到哪些行」仍由服务端 ``ClassroomQuerySet.visible_to / manageable_by`` 决定。
    """

    required_roles = ("teacher", "admin", "student")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # 学生看到的是「我的班级」，不算教学页；管理员进入才挂「代管」标记
        context["is_teacher_page"] = bool(getattr(self.request.user, "is_teacher", False))
        context["is_delegated"] = bool(getattr(self.request.user, "is_admin", False))
        return context


__all__ = [
    "AdminConsoleMixin",
    "AdminRequiredMixin",
    "ClassroomAccessMixin",
    "LoginRequiredMixin",
    "RoleRequiredMixin",
    "StudentRequiredMixin",
    "TeacherPageMixin",
    "TeacherRequiredMixin",
]
