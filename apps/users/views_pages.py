"""
页面视图（服务端渲染）。

与 API 的分工：
  * 页面：Django 模板 + Session 会话认证，负责「看得见、点得动」；
  * 接口：DRF + JWT，负责前后端分离 / 小程序 / 第三方对接。

两种认证方式共用同一套用户模型与业务服务（UserService），不会出现两套逻辑。
"""

import logging

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponseRedirect
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.generic import TemplateView
from django.views.decorators.http import require_POST

from apps.common.exceptions import BusinessError

from .backends import UsernameOrEmailBackend
from .forms import ChangePasswordForm, LoginForm, ProfileForm, RegisterForm
from .services import HomeDashboardService, UserService

logger = logging.getLogger("apps.users.pages")


def _client_ip(request) -> str:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


class HomeView(TemplateView):
    """首页。

    未登录 → 展示系统介绍与登录入口；
    管理员 → 直接 302 到管理控制台（AC-02 / P0-3）；
    教师 / 学生 → 进入各自的工作台，内容与改造前完全一致（AC-01 / AC-11）。
    """

    template_name = "index.html"

    def get(self, request, *args, **kwargs):
        # 管理员不再看到教学卡片墙：默认落地 /manage/（导航里已无教师业务一级项）
        if request.user.is_authenticated and getattr(request.user, "is_admin", False):
            return redirect("manage-dashboard")
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "首页"

        if self.request.user.is_authenticated:
            # 二次设计：首页不再是「导航的卡片版」，而是数据驱动的工作台
            context["dashboard"] = HomeDashboardService.build(self.request.user)
        return context

    @staticmethod
    def _quick_links(user):
        """按角色给出工作台入口。

        **D-1 修复点**：管理员分支必须写在教师分支之前。
        历史上这里先判 ``user.is_teacher``，而管理员天然 is_teacher=True
        （apps/users/models.py:78-80），于是文件末尾的管理员分支永不可达、成了死代码。
        """
        common = [
            {"title": "个人中心", "desc": "维护资料与密码", "url": "/profile/", "icon": "bi-person-gear"},
        ]
        if getattr(user, "is_admin", False):
            # 防御性分支：正常情况下管理员已由 get() 重定向到 /manage/，走不到这里
            return [
                {"title": "数据总览", "desc": "系统级指标、待办与 AI 用量",
                 "url": "/manage/", "icon": "bi-speedometer2"},
                {"title": "用户管理", "desc": "账号治理：新建 / 编辑 / 重置密码 / 停用",
                 "url": "/manage/users/", "icon": "bi-people"},
                {"title": "组织与基础数据", "desc": "班级一览、学科分布、教师学科绑定",
                 "url": "/manage/org/", "icon": "bi-diagram-2"},
                {"title": "系统配置", "desc": "AI Provider 诊断与运行环境",
                 "url": "/manage/system/", "icon": "bi-sliders"},
            ] + common
        if getattr(user, "role", None) == "teacher":
            return [
                {"title": "题库管理", "desc": "六种题型增删改查、Excel 批量导入、AI 智能出题",
                 "url": "/question-bank/", "icon": "bi-journal-text"},
                {"title": "知识点管理", "desc": "维护知识点树，组织题目与学情维度",
                 "url": "/knowledge-points/", "icon": "bi-diagram-3"},
                {"title": "智能组卷", "desc": "AI 按知识点与难度组卷，三栏工作台",
                 "url": "/papers/", "icon": "bi-file-earmark-plus"},
                {"title": "考试管理", "desc": "发布考试、实时监控答题进度",
                 "url": "/exams/", "icon": "bi-pencil-square"},
                {"title": "批改与成绩", "desc": "主观题 AI 批改复核、班级成绩单",
                 "url": "/grading/", "icon": "bi-clipboard-check"},
                {"title": "学情分析", "desc": "班级掌握度、薄弱知识点与成绩趋势",
                 "url": "/analytics/", "icon": "bi-graph-up-arrow"},
                {"title": "作弊检测", "desc": "行为埋点 + 规则引擎 + 孤立森林，可疑考生与证据链",
                 "url": "/proctoring/", "icon": "bi-shield-exclamation"},
                {"title": "班级管理", "desc": "建班、学生分班与名单管理，成绩单与学情按班聚合",
                 "url": "/classrooms/", "icon": "bi-people"},
            ] + common
        if getattr(user, "role", None) == "student":
            return [
                {"title": "我的考试", "desc": "待考与已考列表，进入答题页",
                 "url": "/my-exams/", "icon": "bi-pencil-square"},
                {"title": "错题本", "desc": "按知识点归类的做错题目，附解析",
                 "url": "/my-wrong-book/", "icon": "bi-clipboard-x"},
                {"title": "我的学情", "desc": "掌握度雷达图、薄弱知识点与成绩趋势",
                 "url": "/analytics/me/", "icon": "bi-person-badge"},
                {"title": "每日练习", "desc": "基于薄弱知识点的个性化推荐练习",
                 "url": "/practice/", "icon": "bi-lightning-charge"},
                {"title": "我的班级", "desc": "查看所在班级与同学名单",
                 "url": "/classrooms/", "icon": "bi-people"},
            ] + common
        return common


class SafeNextMixin:
    """处理 ``?next=`` 跳转，并防止被用来做开放重定向钓鱼。"""

    def get_success_url(self) -> str:
        next_url = self.request.POST.get("next") or self.request.GET.get("next")
        if next_url and url_has_allowed_host_and_scheme(
            next_url, allowed_hosts={self.request.get_host()}, require_https=self.request.is_secure()
        ):
            return next_url
        # 管理员默认落地管理控制台（AC-08 的落地口径）：登录成功后不再回到教师/学生工作台
        user = getattr(self.request, "user", None)
        if user is not None and user.is_authenticated and getattr(user, "is_admin", False):
            return reverse("manage-dashboard")
        return reverse("home")


# ==================================================================
# 登录 / 注册 / 登出
# ==================================================================
class LoginPageView(SafeNextMixin, TemplateView):
    """GET 渲染登录页；POST 完成登录。"""

    template_name = "users/login.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect(self.get_success_url())
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        kwargs.setdefault("form", LoginForm())
        kwargs.setdefault("next", self.request.GET.get("next", ""))
        kwargs["page_title"] = "登录"
        return super().get_context_data(**kwargs)

    def post(self, request, *args, **kwargs):
        form = LoginForm(request.POST)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))

        user = authenticate(
            request,
            username=form.cleaned_data["username"],
            password=form.cleaned_data["password"],
        )
        if user is None:
            # D-4：认证后端对停用账号返回 None（user_can_authenticate=False），
            # 「已停用」分支原本永远不可达。这里做**二次判定**：
            # 只有「账号存在 + 密码正确 + 已停用」才给出停用文案，
            # 账号不存在 / 密码错误仍统一提示，不引入账号枚举（AC-26 / AC-27）。
            candidate = UsernameOrEmailBackend.resolve_user(form.cleaned_data["username"])
            if (
                candidate is not None
                and candidate.check_password(form.cleaned_data["password"])
                and not candidate.is_active
            ):
                form.add_error(None, "该账号已被停用，请联系管理员")
            else:
                form.add_error(None, "账号或密码错误，请重新输入")
            return self.render_to_response(self.get_context_data(form=form))

        login(request, user)
        # 未勾选「记住我」→ 关闭浏览器即失效
        if not form.cleaned_data.get("remember_me"):
            request.session.set_expiry(0)

        UserService.record_login(user, _client_ip(request))
        logger.info("用户登录: %s", user.username)
        messages.success(request, f"欢迎回来，{user.display_name}！")
        return redirect(self.get_success_url())


class RegisterPageView(SafeNextMixin, TemplateView):
    """GET 渲染注册页；POST 完成注册并自动登录。"""

    template_name = "users/register.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("home")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        kwargs.setdefault("form", RegisterForm())
        kwargs["page_title"] = "注册"
        return super().get_context_data(**kwargs)

    def post(self, request, *args, **kwargs):
        form = RegisterForm(request.POST)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))

        data = form.cleaned_data
        user = UserService.register(
            username=data["username"],
            password=data["password"],
            email=data["email"],
            role=data.get("role") or "student",
            real_name=data.get("real_name", ""),
            grade_year=data.get("grade_year"),
            phone=data.get("phone", ""),
            school=data.get("school", ""),
        )
        # 项目配置了多个认证后端（用户名/邮箱/学号 + Admin 兜底），
        # 注册用户没经过 authenticate()，必须显式指定 backend 才能建立会话
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        messages.success(request, "注册成功，已为你自动登录")
        return redirect("home")


@require_POST
def logout_page(request):
    """退出登录（POST，避免被 GET 预取误触发）。"""
    if request.user.is_authenticated:
        logger.info("用户登出: %s", request.user.username)
    logout(request)
    messages.info(request, "你已安全退出")
    return redirect("login-page")


# ==================================================================
# 个人中心
# ==================================================================
class ProfilePageView(LoginRequiredMixin, TemplateView):
    """个人资料：查看 + 修改 + 改密，一个页面搞定。"""

    template_name = "users/profile.html"
    login_url = "/login/"

    def get_context_data(self, **kwargs):
        kwargs.setdefault("form", ProfileForm(instance=self.request.user))
        kwargs.setdefault("password_form", ChangePasswordForm())
        kwargs["page_title"] = "个人中心"
        return super().get_context_data(**kwargs)

    def post(self, request, *args, **kwargs):
        action = request.POST.get("action", "profile")

        if action == "password":
            return self._change_password(request)
        return self._update_profile(request)

    def _update_profile(self, request):
        form = ProfileForm(request.POST, request.FILES, instance=request.user)
        if not form.is_valid():
            messages.error(request, "资料保存失败，请检查填写内容")
            return self.render_to_response(
                self.get_context_data(form=form, password_form=ChangePasswordForm())
            )
        form.save()
        messages.success(request, "个人资料已更新")
        return HttpResponseRedirect(reverse("profile-page"))

    def _change_password(self, request):
        form = ChangePasswordForm(request.POST)
        if not form.is_valid():
            messages.error(request, "密码修改失败，请检查填写内容")
            return self.render_to_response(
                self.get_context_data(form=ProfileForm(instance=request.user), password_form=form)
            )
        try:
            UserService.change_password(
                request.user, form.cleaned_data["old_password"], form.cleaned_data["new_password"]
            )
        except BusinessError as exc:
            form.add_error("old_password", str(exc.detail))
            return self.render_to_response(
                self.get_context_data(form=ProfileForm(instance=request.user), password_form=form)
            )
        # 改密后保持当前会话有效（JWT 那边的 refresh token 会被拉黑）
        update_session_auth_hash(request, request.user)
        messages.success(request, "密码修改成功")
        return HttpResponseRedirect(reverse("profile-page"))
