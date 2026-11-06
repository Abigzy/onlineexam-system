"""管理端「用户管理」页面视图（服务端渲染）。

为什么放在 ``apps/users`` 而不是 ``apps/system``：
  用户账号的领域知识（角色、学科、唯一性、最后管理员护栏）都在本 app，
  页面若搬到 system 会逼着 system 反向依赖 users 的服务层，职责更乱。
  ``apps/system`` 只负责「控制台外壳与跨模块只读聚合」。

三条实现纪律：
  1. 列表筛选复用 ``UserFilter``（含已加的 ``subject``），不另写一套查询条件；
  2. 学科候选来自库内 distinct 值（``SystemOverviewService.subject_options``），
     与接口 ``/api/v1/users/?subject=`` 完全同源；
  3. 危险动作（重置密码 / 停用）**不在页面上直接做**，由 ``static/js/manage.js``
     调既有 API，保证「账号治理」只有一份实现（服务层）。
"""

import logging
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.generic import TemplateView

from apps.classroom.constants import ClassType
from apps.classroom.models import Classroom
from apps.common.constants import Role
from apps.common.mixins import AdminConsoleMixin
from apps.system.services import SystemOverviewService

from .filters import UserFilter
from .forms_manage import AdminUserCreateForm, AdminUserEditForm

logger = logging.getLogger("apps.users.pages_manage")

User = get_user_model()

# 每页条数：与后端 StandardPagination 的 max_page_size=100 对齐（PRD §4.1）
PAGE_SIZE_CHOICES = (10, 20, 50, 100)
DEFAULT_PAGE_SIZE = 10


def _active_admin_queryset():
    """「启用状态的管理员」口径（U-2）：role=admin 或 is_superuser。"""
    return User.objects.filter(is_active=True).filter(Q(role=Role.ADMIN) | Q(is_superuser=True))


def _attach_student_subjects(users):
    """给学生对象挂 ``subject_list``：其所在**学科班**的授课学科（去重、去空、排序）。

    学生的「学科」不是一个字段，而是 ``user.classrooms`` 里学科班的 ``Classroom.subject``。
    这里只对当前页的学生做**一次**查询，再在内存里按 user_id 分组，
    避免在模板里对每行反查（N+1）。
    """
    student_ids = [user.pk for user in users if user.role == Role.STUDENT]
    collected = {}
    if student_ids:
        rows = (
            Classroom.objects.filter(
                class_type=ClassType.SUBJECT, students__in=student_ids
            )
            .order_by("students__id", "subject")
            .values_list("students__id", "subject")
        )
        for user_id, subject in rows:
            subject = (subject or "").strip()
            # 同一学科可能挂在多个学科班（如「数据结构 1 班 / 2 班」），按 user 去重
            if subject:
                collected.setdefault(user_id, set()).add(subject)

    for user in users:
        user.subject_list = sorted(collected.get(user.pk, ()))
    return users


def _classroom_options():
    """启用中的**全部**班级（id / name / class_type / subject / 学生数），供筛选下拉使用。

    行政班与学科班都要能筛，所以不按 ``class_type`` 过滤；模板按 ``class_type``
    分成两个 ``optgroup`` 渲染，故这里连同 ``subject``（学科班的授课学科）一起取回。
    ``annotate(Count("students"))`` 把人数算在 SQL 里，
    避免模板里对每个班级调 ``student_count`` 属性触发 N+1。
    """
    return list(
        Classroom.objects.filter(is_active=True)
        .values("id", "name", "class_type", "subject")
        .annotate(student_count=Count("students"))
        .order_by("class_type", "name")
    )


class ManageUserListPageView(AdminConsoleMixin, TemplateView):
    """``GET /manage/users/`` 账号治理主界面（AC-12 / AC-13）。"""

    template_name = "users/manage_list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        request = self.request

        page_size = self._page_size(request)
        # 关键词搜索用 QuerySet.search()（用户名 / 姓名 / 邮箱 / 学号），
        # 参数名按 PRD 叫 search=；不塞进 UserFilter，避免与 DRF 的 SearchFilter 重复生效。
        queryset = User.objects.all()
        keyword = (request.GET.get("search") or "").strip()
        if keyword:
            queryset = queryset.search(keyword)

        # 传 None 让未带任何参数时表单是「未绑定」状态，模板里不会显示空错误
        filterset = UserFilter(request.GET or None, queryset=queryset)
        queryset = filterset.qs.order_by("-date_joined")

        paginator = Paginator(queryset, page_size)
        page_obj = paginator.get_page(request.GET.get("page"))

        # 学生「加入了哪些学科」只能从学科班反查：只算当前页，一次查询搞定（防 N+1）。
        _attach_student_subjects(page_obj.object_list)

        # 下拉分两组渲染；某一类班级为空时，模板不渲染空的 optgroup。
        classroom_options = _classroom_options()

        # AC-18 的页面文案：只有 1 个启用管理员时，提前把该账号标出来，
        # 由模板把「停用」按钮置灰并给出明确原因，而不是等接口回 400。
        last_admin_id = None
        if _active_admin_queryset().count() == 1:
            last_admin_id = _active_admin_queryset().values_list("pk", flat=True).first()

        context.update({
            "page_title": "用户管理",
            "active_menu": "manage-users",
            "filter": filterset,
            "page_obj": page_obj,
            "paginator": paginator,
            # 省略式页码范围：一万条数据下也不会渲染出上千个分页按钮
            "page_range": paginator.get_elided_page_range(
                page_obj.number, on_each_side=1, on_ends=1
            ),
            "total_count": paginator.count,
            "page_size": page_size,
            "page_size_choices": PAGE_SIZE_CHOICES,
            "querystring": self._querystring(request),
            "subjects": SystemOverviewService.subject_options(),
            "classroom_options": classroom_options,
            "has_admin_classrooms": any(
                item["class_type"] == ClassType.ADMIN for item in classroom_options
            ),
            "has_subject_classrooms": any(
                item["class_type"] == ClassType.SUBJECT for item in classroom_options
            ),
            "role_choices": list(Role.choices),
            "self_id": request.user.pk,
            "last_admin_id": last_admin_id,
            "has_any_user": User.objects.exists(),
            "is_filtered": bool(request.GET),
        })
        return context

    @staticmethod
    def _page_size(request) -> int:
        raw = request.GET.get("page_size")
        if raw and str(raw).isdigit() and int(raw) in PAGE_SIZE_CHOICES:
            return int(raw)
        return DEFAULT_PAGE_SIZE

    @staticmethod
    def _querystring(request) -> str:
        """当前筛选条件（去掉 page），供分页链接拼接，保证翻页不丢筛选。"""
        params = request.GET.copy()
        params.pop("page", None)
        encoded = params.urlencode()
        return f"{encoded}&" if encoded else ""


class ManageUserCreatePageView(AdminConsoleMixin, TemplateView):
    """``GET/POST /manage/users/new/`` 管理员直接建号（AC-14 / AC-15）。"""

    template_name = "users/manage_form.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.setdefault("form", AdminUserCreateForm())
        context.update({
            "page_title": "新建用户",
            "active_menu": "manage-users",
            "mode": "create",
            "target": None,
            "subjects": SystemOverviewService.subject_options(),
        })
        return context

    def post(self, request, *args, **kwargs):
        form = AdminUserCreateForm(request.POST)
        if not form.is_valid():
            # 不跳转：错误留在原输入框下方，已填内容不丢失（AC-14）
            messages.error(request, "创建失败，请检查表单中标红的字段")
            return self.render_to_response(self.get_context_data(form=form))

        user = form.save()
        messages.success(request, f"用户 {user.username} 创建成功")
        logger.info("管理员建号成功: %s (%s)", user.username, user.get_role_display())
        return redirect(f"{reverse('manage-user-list')}?{urlencode({'search': user.username})}")


class ManageUserEditPageView(AdminConsoleMixin, TemplateView):
    """``GET/POST /manage/users/<id>/edit/`` 编辑资料 / 角色 / 学科（AC-16 / AC-17）。"""

    template_name = "users/manage_form.html"

    def get_target(self):
        return get_object_or_404(User, pk=self.kwargs["pk"])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        target = self.get_target()
        context.setdefault("form", AdminUserEditForm(instance=target))
        context.update({
            "page_title": "编辑用户",
            "active_menu": "manage-users",
            "mode": "edit",
            "target": target,
            "subjects": SystemOverviewService.subject_options(),
            "is_self": target.pk == self.request.user.pk,
        })
        return context

    def post(self, request, *args, **kwargs):
        target = self.get_target()
        form = AdminUserEditForm(request.POST, instance=target)
        if not form.is_valid():
            messages.error(request, "保存失败，请检查表单中标红的字段")
            return self.render_to_response(self.get_context_data(form=form))

        form.save()
        messages.success(request, f"用户 {target.username} 的资料已更新")
        logger.info("管理员修改用户资料: %s", target.username)
        return redirect(f"{reverse('manage-user-list')}?{urlencode({'search': target.username})}")


__all__ = [
    "ManageUserCreatePageView",
    "ManageUserEditPageView",
    "ManageUserListPageView",
]
