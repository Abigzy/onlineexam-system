"""全局中间件。

目前只有一个：``InactiveSessionGuardMiddleware``——账号被停用后即时踢下线（AC-25）。

为什么放在中间件而不是每个视图里判断：
  * 已登录的页面会话在账号被停用后仍然持有有效的 session cookie，
    Django 只是「悄悄」把它当匿名用户处理（见下方说明），用户会莫名其妙被弹回登录页，
    且提示是「请先登录」——既不解释原因，也没有真正注销会话；
  * 集中一处复核，比在几十个视图里重复判断可靠。

为什么不会影响 JWT 接口：
  * SimpleJWT 在**视图层**认证，中间件阶段 ``request.user`` 还是 ``AnonymousUser``，
    JWT 请求也不带 session cookie，天然进不了下面的分支；
  * JWT 侧的 ``is_active`` 由 SimpleJWT 自己校验（旧 token 下一次请求即 401，AC-28）。

**关键实现说明（务必保留第二段判断）**：
本项目所有认证后端都继承 ``ModelBackend``，其 ``get_user()`` 会调用
``user_can_authenticate()``，对 ``is_active=False`` 的账号返回 ``None``。
于是 ``AuthenticationMiddleware`` 把 ``request.user`` 直接置成 ``AnonymousUser``——
也就是说「停用后页面会话仍能继续点」在本项目里表现为「被当成匿名用户」，
单看 ``request.user`` 是**发现不了**停用账号的，必须回查 session 里的用户 id。
"""

import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import SESSION_KEY, get_user_model, logout
from django.http import JsonResponse
from django.shortcuts import redirect

from apps.common.constants import CODE_ACCOUNT_INACTIVE

logger = logging.getLogger("apps.common.middleware")

User = get_user_model()

# 这些前缀不做停用复查：
#   * 登录页 / 登出：否则停用账号会被重定向死循环；
#   * 静态与媒体资源：避免每个 css/js 都走一遍会话查询；
#   * 接口文档与健康检查：无需登录态语义。
SKIP_PREFIXES = (
    "/login/",
    "/logout/",
    "/static/",
    "/media/",
    "/api/docs/",
    "/api/schema/",
    "/api/health/",
)

# 文案与登录接口 / 页面保持逐字一致（P4 统一）：同一个「账号停用」语义，
# 两条通道（中间件 / 登录接口）不能差一个字，否则前端做文案判重时会漏。
INACTIVE_MESSAGE = "该账号已被停用，请联系管理员"
# 与登录接口（apps/users/serializers._inactive_error）共用同一个业务码，避免漂移
INACTIVE_BIZ_CODE = CODE_ACCOUNT_INACTIVE


class InactiveSessionGuardMiddleware:
    """Session 会话的 ``is_active`` 复查：命中停用 → 注销 + 跳登录（接口返回 401）。"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path or ""
        if path.startswith(SKIP_PREFIXES):
            return self.get_response(request)

        if self._session_belongs_to_inactive_user(request):
            return self._reject(request, path)

        return self.get_response(request)

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------
    def _session_belongs_to_inactive_user(self, request) -> bool:
        """判断当前请求是否来自「已停用但仍持有会话 cookie」的账号。

        两条判定路径，任一命中即视为停用：
          1. ``request.user`` 已认证但 ``is_active=False``
             （未来若接入不校验 ``is_active`` 的认证后端，这条会先命中）；
          2. ``request.user`` 未认证，但 session 里记着某个用户 id，且该用户已被停用
             （本项目 ``ModelBackend.get_user()`` 的默认行为会落到这一条）。
        """
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            return not user.is_active

        # 只有「带着会话 cookie 却认证失败」才需要回查，
        # 纯匿名访客（无 cookie）在这里就返回 False，不产生任何查询。
        if settings.SESSION_COOKIE_NAME not in request.COOKIES:
            return False

        user_id = request.session.get(SESSION_KEY)
        if not user_id:
            return False
        is_active = User.objects.filter(pk=user_id).values_list("is_active", flat=True).first()
        return is_active is False

    # ------------------------------------------------------------------
    # 拒绝
    # ------------------------------------------------------------------
    def _reject(self, request, path: str):
        username = request.session.get("_auth_user_id", "")
        # 先注销再写提示：logout() 会 flush 会话，提示必须写在它之后。
        logout(request)
        logger.info("停用账号的会话请求被拦截并注销: user_id=%s | %s", username, path)

        if path.startswith("/api/"):
            return JsonResponse(
                {"code": INACTIVE_BIZ_CODE, "message": INACTIVE_MESSAGE, "data": None},
                status=401,
            )

        messages.error(request, INACTIVE_MESSAGE)
        return redirect("login-page")
