"""
用户与认证的 RESTful 接口。

路由总览（全部挂在 /api/v1/ 下）：
    POST   /auth/register/          注册
    POST   /auth/login/             登录（返回 access + refresh）
    POST   /auth/refresh/           刷新 access
    POST   /auth/logout/            登出（拉黑 refresh）
    GET    /auth/me/                当前登录用户
    PATCH  /auth/me/                修改自己的资料
    POST   /auth/change-password/   修改密码

    GET    /users/                  用户列表（管理员）
    POST   /users/                  新建用户（管理员）
    GET    /users/{id}/             用户详情
    PUT    /users/{id}/             全量更新（管理员）
    PATCH  /users/{id}/             局部更新（管理员）
    DELETE /users/{id}/             删除（管理员）
    GET    /users/teachers/         教师列表
    GET    /users/students/         学生列表
    POST   /users/{id}/reset-password/  重置密码（管理员）
    POST   /users/{id}/toggle-active/   启用 / 停用（管理员）
"""

import logging

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from apps.common.constants import Role
from apps.common.exceptions import BusinessError
from apps.common.pagination import StandardPagination
from apps.common.permissions import IsAdmin
from apps.common.response import ApiResponse, success

from .filters import UserFilter
from .serializers import (
    AdminUserSerializer,
    ChangePasswordSerializer,
    CustomTokenObtainPairSerializer,
    LogoutSerializer,
    UserRegisterSerializer,
    UserSerializer,
    UserUpdateSerializer,
)
from .services import UserService

logger = logging.getLogger("apps.users.views")
User = get_user_model()


# ==================================================================
# 认证：/api/v1/auth/
# ==================================================================
@extend_schema(tags=["认证"], summary="用户注册")
class RegisterView(viewsets.GenericViewSet):
    """POST /api/v1/auth/register/"""

    serializer_class = UserRegisterSerializer
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "auth"

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = UserService.register(
            username=data["username"],
            password=data["password"],
            email=data["email"],
            role=data.get("role", Role.STUDENT),
            real_name=data.get("real_name", ""),
            grade_year=data.get("grade_year"),
            phone=data.get("phone", ""),
            school=data.get("school", ""),
        )

        return ApiResponse(
            data=UserSerializer(user, context={"request": request}).data,
            message="注册成功，请使用新账号登录",
            status_code=status.HTTP_201_CREATED,
        )


@extend_schema(tags=["认证"], summary="登录（用户名 / 邮箱 / 学号）")
class LoginView(TokenObtainPairView):
    """POST /api/v1/auth/login/  → {access, refresh, user}"""

    serializer_class = CustomTokenObtainPairSerializer
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "auth"


@extend_schema(tags=["认证"], summary="刷新 access token")
class RefreshView(TokenRefreshView):
    """POST /api/v1/auth/refresh/  body: {refresh}"""

    permission_classes = [AllowAny]
    authentication_classes = []


@extend_schema(tags=["认证"], summary="登出（refresh token 拉黑）")
class LogoutView(viewsets.GenericViewSet):
    """POST /api/v1/auth/logout/  body: {refresh}"""

    serializer_class = LogoutSerializer
    permission_classes = [IsAuthenticated]

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            RefreshToken(serializer.validated_data["refresh"]).blacklist()
        except TokenError:
            # token 已失效也视为登出成功，避免前端卡在登录态
            logger.info("登出时 token 已失效，忽略：user=%s", request.user.username)
        return success(message="已退出登录")


@extend_schema(tags=["认证"], summary="当前登录用户：查询 / 修改资料")
class MeView(viewsets.GenericViewSet):
    """GET / PATCH / PUT /api/v1/auth/me/"""

    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        return UserSerializer if self.request.method == "GET" else UserUpdateSerializer

    def list(self, request):
        return success(data=UserSerializer(request.user, context={"request": request}).data)

    def partial_update(self, request, *args, **kwargs):
        return self._update(request, partial=True)

    def update(self, request, *args, **kwargs):
        return self._update(request, partial=False)

    def _update(self, request, partial: bool):
        serializer = UserUpdateSerializer(
            request.user, data=request.data, partial=partial, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return success(
            data=UserSerializer(request.user, context={"request": request}).data,
            message="资料已更新",
        )


@extend_schema(tags=["认证"], summary="修改密码")
class ChangePasswordView(viewsets.GenericViewSet):
    """POST /api/v1/auth/change-password/"""

    serializer_class = ChangePasswordSerializer
    permission_classes = [IsAuthenticated]

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        UserService.change_password(request.user, data["old_password"], data["new_password"])
        return success(message="密码修改成功，请重新登录")


# ==================================================================
# 用户资源：/api/v1/users/
# ==================================================================
@extend_schema(tags=["用户管理"])
class UserViewSet(viewsets.ModelViewSet):
    """用户资源的增删改查（默认仅管理员可用）。"""

    queryset = User.objects.all()
    serializer_class = UserSerializer
    permission_classes = [IsAdmin]
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = UserFilter
    search_fields = ["username", "real_name", "email", "student_no", "phone"]
    ordering_fields = ["date_joined", "last_login", "username", "id"]
    ordering = ["-date_joined"]

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update"):
            return AdminUserSerializer
        return UserSerializer

    def get_queryset(self):
        # select_related 用不上（无外键），但固定排序避免分页不稳定
        return User.objects.all().order_by("-date_joined")

    def create(self, request, *args, **kwargs):
        """管理员直接建号：需要带初始密码，且必须通过 AUTH_PASSWORD_VALIDATORS。

        学号 / 工号管理员可以手填（不受注册入口的限制）；没填则自动派发：
        教师派工号，学生按 ``grade_year`` 派学号。
        """
        serializer = AdminUserSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        password = str(request.data.get("password") or "").strip()
        if not password or len(password) < 6:
            raise BusinessError("请提供至少 6 位的初始密码", code=10006)
        try:
            validate_password(password)
        except DjangoValidationError as exc:
            raise BusinessError("；".join(exc.messages), code=10011)

        data = serializer.validated_data
        role = data.get("role") or Role.STUDENT
        student_no = (data.get("student_no") or "").strip()
        grade_year = str(request.data.get("grade_year") or "").strip()

        # 学生必须提供年级：学号既靠它派发，也靠它校验手填学号没有跨年级
        if role == Role.STUDENT:
            if not grade_year:
                raise BusinessError("创建学生必须提供入学年级（grade_year）", code=10014)
            if student_no and not UserService.student_no_matches_grade(student_no, grade_year):
                raise BusinessError(
                    f"学号前 4 位（{student_no[:4]}）与入学年级（{grade_year}）不一致",
                    code=10015,
                )

        save_kwargs = {}
        if not student_no:
            if role == Role.TEACHER:
                save_kwargs["student_no"] = UserService.generate_employee_no()
            elif role == Role.STUDENT:
                save_kwargs["student_no"] = UserService.generate_student_no(grade_year)

        user = serializer.save(**save_kwargs)
        user.set_password(password)
        user.save(update_fields=["password"])
        return ApiResponse(
            data=AdminUserSerializer(user, context={"request": request}).data,
            message="用户创建成功",
            status_code=status.HTTP_201_CREATED,
        )

    def destroy(self, request, *args, **kwargs):
        user = self.get_object()
        if user == request.user:
            raise BusinessError("不能删除当前登录的账号", code=10007)
        if user.is_superuser:
            raise BusinessError("超级管理员账号不可删除", code=10008, status_code=403)
        username = user.username
        user.delete()
        return success(message=f"用户 {username} 已删除")

    # ---------------- 自定义动作 ----------------
    @extend_schema(tags=["用户管理"], summary="教师列表")
    @action(detail=False, methods=["get"], url_path="teachers")
    def teachers(self, request):
        return self._list_by_role(Role.TEACHER)

    @extend_schema(tags=["用户管理"], summary="学生列表")
    @action(detail=False, methods=["get"], url_path="students")
    def students(self, request):
        return self._list_by_role(Role.STUDENT)

    def _list_by_role(self, role: str):
        queryset = self.filter_queryset(self.get_queryset().filter(role=role))
        page = self.paginate_queryset(queryset)
        serializer = UserSerializer(page if page is not None else queryset, many=True,
                                    context=self.get_serializer_context())
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return success(data=serializer.data)

    @extend_schema(tags=["用户管理"], summary="重置密码（管理员）")
    @action(detail=True, methods=["post"], url_path="reset-password")
    def reset_password(self, request, pk=None):
        """重置某账号的密码。

        接口签名保持不变（仍是 ``POST /users/{id}/reset-password/``）：
          * body 不带 ``new_password`` → 服务端随机生成 12 位强密码（AC-19）；
          * body 带 ``new_password`` → 必须通过 ``AUTH_PASSWORD_VALIDATORS``（AC-20）。

        明文**只出现在 ``data.password`` 里一次**，绝不拼进 ``message``
        （旧实现会把它写进 toast 与日志，等同于二次泄露，D-3 / 陷阱 2）。
        """
        user = self.get_object()
        new_password = str(request.data.get("new_password") or "").strip() or None
        try:
            plain = UserService.admin_reset_password(user, new_password)
        except DjangoValidationError as exc:
            raise BusinessError("；".join(exc.messages), code=10011)
        return success(data={"password": plain}, message="密码已重置")

    @extend_schema(tags=["用户管理"], summary="启用 / 停用账号")
    @action(detail=True, methods=["post"], url_path="toggle-active")
    def toggle_active(self, request, pk=None):
        user = self.get_object()
        if user == request.user:
            raise BusinessError("不能停用自己的账号", code=10010)
        # 护栏（防御性冗余）：不允许把「最后一个启用管理员」停用（AC-18）。
        # 说明：能调本接口的人自己必然是启用状态的管理员，因此 remaining ≥ 1 恒成立，
        # 这条判断在接口层几乎不可达；真正拦住「停用最后一个管理员」的是上面那条
        # 「不能停用自己」。保留它是为了让服务层口径（UserService.assert_admin_survives）
        # 只有一处实现，将来若放开「管理员之间互相停用」也无需补代码。
        UserService.assert_admin_survives(user, is_active=not user.is_active)
        user.is_active = not user.is_active
        user.save(update_fields=["is_active"])
        return success(
            data={"is_active": user.is_active},
            message="账号已启用" if user.is_active else "账号已停用",
        )
