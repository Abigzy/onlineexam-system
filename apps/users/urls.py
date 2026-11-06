"""用户模块 API 路由。

标准 RESTful 语义：
    GET    列表 / 详情      安全且幂等
    POST   创建 / 动作      非幂等
    PUT    全量更新         幂等
    PATCH  局部更新         幂等
    DELETE 删除             幂等
"""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    ChangePasswordView,
    LoginView,
    LogoutView,
    MeView,
    RefreshView,
    RegisterView,
    UserViewSet,
)

app_name = "users_api"

# ---------------- 认证动作 ----------------
auth_patterns = [
    path("register/", RegisterView.as_view({"post": "create"}), name="register"),
    path("login/", LoginView.as_view(), name="login"),
    path("refresh/", RefreshView.as_view(), name="refresh"),
    path("logout/", LogoutView.as_view({"post": "create"}), name="logout"),
    path(
        "me/",
        MeView.as_view({"get": "list", "put": "update", "patch": "partial_update"}),
        name="me",
    ),
    path(
        "change-password/",
        ChangePasswordView.as_view({"post": "create"}),
        name="change-password",
    ),
]

# ---------------- 用户资源 ----------------
router = DefaultRouter()
router.register("", UserViewSet, basename="user")

urlpatterns = [
    path("auth/", include(auth_patterns)),
    path("users/", include(router.urls)),
]
