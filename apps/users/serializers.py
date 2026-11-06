"""
用户模块序列化器。

约定：
  * 读序列化器（XxxSerializer）绝不暴露 password 等 write_only 字段；
  * 写序列化器（XxxCreate/UpdateSerializer）单独定义，字段白名单明确；
  * 字段级错误交给 DRF 自动收集，跨字段校验写在 ``validate()`` 里。
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.db.models import Q
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from apps.common.constants import CODE_ACCOUNT_INACTIVE, CODE_AUTH_FAILED, Role
from apps.common.exceptions import BusinessError

from .constants import grade_year_choices
from .services import UserService

INACTIVE_MESSAGE = "该账号已被停用，请联系管理员"
LOGIN_FAILED_MESSAGE = "账号或密码错误，请重新输入"

User = get_user_model()


# ==================================================================
# 读
# ==================================================================
class UserSerializer(serializers.ModelSerializer):
    """用户公开信息（用于返回给前端渲染）。"""

    role_display = serializers.CharField(source="get_role_display", read_only=True)
    display_name = serializers.CharField(read_only=True)
    avatar_url = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id", "username", "email", "real_name", "display_name",
            "role", "role_display", "phone", "student_no", "school",
            "subject",
            "avatar", "avatar_url", "is_active", "last_login",
            "last_login_ip", "date_joined",
        ]
        read_only_fields = ["id", "username", "role", "is_active", "last_login",
                            "last_login_ip", "date_joined", "avatar_url", "subject"]

    def get_avatar_url(self, obj) -> str:
        """返回可直接放进 <img src> 的绝对地址。"""
        if not obj.avatar:
            return ""
        request = self.context.get("request")
        url = obj.avatar.url
        return request.build_absolute_uri(url) if request else url


class UserBriefSerializer(serializers.ModelSerializer):
    """极简信息，用于嵌套（如考试的创建人、班级成员列表）。"""

    display_name = serializers.CharField(read_only=True)

    class Meta:
        model = User
        fields = ["id", "username", "display_name", "role", "student_no"]


# ==================================================================
# 注册 / 修改
# ==================================================================
class UserRegisterSerializer(serializers.ModelSerializer):
    """注册。密码做二次确认 + Django 内置强度校验。

    学号 / 工号**不接收客户端输入**（管理员建号走 ``AdminUserSerializer``）：
    学生按所选年级由系统派发学号，教师账号注册入口本就不开放。
    """

    password = serializers.CharField(
        write_only=True, min_length=6, style={"input_type": "password"},
        error_messages={"min_length": "密码长度不能少于 6 位"},
    )
    password2 = serializers.CharField(
        write_only=True, style={"input_type": "password"}, label="确认密码",
    )
    role = serializers.ChoiceField(
        choices=[Role.STUDENT],
        default=Role.STUDENT,
        error_messages={"invalid_choice": "教师账号由管理员创建，注册仅支持学生"},
    )
    grade_year = serializers.CharField(
        write_only=True, label="入学年级",
        help_text="4 位年份（如 2026）；系统据此派发学号",
    )

    class Meta:
        model = User
        fields = ["username", "email", "password", "password2",
                  "role", "real_name", "grade_year", "phone", "school"]

    def validate_grade_year(self, value):
        value = (value or "").strip()
        valid = {code for code, _ in grade_year_choices()}
        if value not in valid:
            raise serializers.ValidationError("请选择有效的入学年级")
        return value

    def validate_username(self, value):
        value = value.strip()
        if len(value) < 3:
            raise serializers.ValidationError("用户名长度不能少于 3 位")
        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError("该用户名已被占用")
        return value

    def validate_email(self, value):
        value = value.strip().lower()
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("该邮箱已被注册")
        return value

    def validate_password(self, value):
        validate_password(value)  # 触发 settings.AUTH_PASSWORD_VALIDATORS
        return value

    def validate(self, attrs):
        if attrs.get("password") != attrs.get("password2"):
            raise serializers.ValidationError({"password2": "两次输入的密码不一致"})
        return attrs
        return attrs


class UserUpdateSerializer(serializers.ModelSerializer):
    """用户自助修改资料（不允许改角色、用户名、学号/工号）。

    学号 / 工号由系统派发，是身份标识，自助修改意味着可以冒用别人的号。
    """

    class Meta:
        model = User
        fields = ["email", "real_name", "phone", "school", "avatar"]

    def validate_email(self, value):
        value = value.strip().lower()
        if User.objects.filter(email__iexact=value).exclude(pk=self.instance.pk).exists():
            raise serializers.ValidationError("该邮箱已被其他账号使用")
        return value


class AdminUserSerializer(serializers.ModelSerializer):
    """管理员操作用户的序列化器：可以改角色、学科与启用状态。

    ``subject``（任教学科）由管理端维护（U-1）：教师账号可绑定学科，
    候选人列表来自 ``overview.options.subjects``，但字段本身是自由文本（Q-4）。
    """

    role_display = serializers.CharField(source="get_role_display", read_only=True)

    class Meta:
        model = User
        fields = ["id", "username", "email", "real_name", "role", "role_display",
                  "phone", "student_no", "school", "subject", "avatar",
                  "is_active", "date_joined"]
        read_only_fields = ["id", "date_joined"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 用户名「创建时可填、创建后不可改」（PRD §4.3）。
        # 原先 username 被无条件设为只读，导致管理员建号会落成空用户名；
        # 这里只在 update 场景保持只读。
        if self.instance is not None:
            self.fields["username"].read_only = True

    def validate_username(self, value):
        """用户名：≥3 位、全局唯一（忽略大小写）。"""
        value = (value or "").strip()
        if len(value) < 3:
            raise serializers.ValidationError("用户名长度不能少于 3 位")
        queryset = User.objects.filter(username__iexact=value)
        if self.instance is not None:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise serializers.ValidationError("该用户名已被占用")
        return value

    def validate_email(self, value):
        """邮箱全局唯一（模型层是 unique，放在这里校验才能给出字段级提示）。"""
        value = (value or "").strip().lower()
        queryset = User.objects.filter(email__iexact=value)
        if self.instance is not None:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise serializers.ValidationError("该邮箱已被注册")
        return value

    def validate_student_no(self, value):
        """学号 / 工号全局唯一（与注册入口口径一致）。"""
        value = (value or "").strip()
        if value:
            queryset = User.objects.filter(student_no__iexact=value)
            if self.instance is not None:
                queryset = queryset.exclude(pk=self.instance.pk)
            if queryset.exists():
                raise serializers.ValidationError("该学号 / 工号已被使用")
        return value

    def validate_role(self, value):
        if self.instance and self.instance.is_superuser and value != Role.ADMIN:
            raise serializers.ValidationError("超级管理员的角色不可降级")
        return value

    def validate(self, attrs):
        """护栏：不允许把「最后一个启用管理员」降级（AC-18 / U-2）。"""
        if self.instance is not None and "role" in attrs:
            try:
                UserService.assert_admin_survives(self.instance, role=attrs["role"])
            except BusinessError as exc:  # 业务护栏 → 字段级校验错误
                raise serializers.ValidationError({"role": str(exc.detail)})
        return attrs


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(write_only=True, style={"input_type": "password"}, label="原密码")
    new_password = serializers.CharField(
        write_only=True, min_length=6, style={"input_type": "password"}, label="新密码",
        error_messages={"min_length": "新密码长度不能少于 6 位"},
    )
    confirm_password = serializers.CharField(write_only=True, style={"input_type": "password"}, label="确认新密码")

    def validate_new_password(self, value):
        validate_password(value)
        return value

    def validate(self, attrs):
        if attrs["new_password"] != attrs["confirm_password"]:
            raise serializers.ValidationError({"confirm_password": "两次输入的新密码不一致"})
        if attrs["new_password"] == attrs["old_password"]:
            raise serializers.ValidationError({"new_password": "新密码不能与原密码相同"})
        return attrs


# ==================================================================
# 认证
# ==================================================================
class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    """登录：支持用户名 / 邮箱 / 学号，并在 token 里塞入角色，前端少发一次请求。"""

    username_field = "username"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"] = serializers.CharField(
            label="账号",
            help_text="用户名 / 邮箱 / 学号，三者填其一即可",
        )

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        # 自定义载荷：前端可从 token 直接解出角色用于菜单渲染
        token["username"] = user.username
        token["role"] = user.role
        token["real_name"] = user.display_name
        return token

    def validate(self, attrs):
        login = attrs.get(self.username_field)
        # 把邮箱 / 学号解析回 username，再交给父类做密码校验
        from .backends import UsernameOrEmailBackend

        matched = UsernameOrEmailBackend.resolve_user(login)
        if matched is not None:
            attrs[self.username_field] = matched.get_username()

        try:
            data = super().validate(attrs)
        except AuthenticationFailed:
            # 登录失败统一出口（AC-26 / AC-27 / U-3）：
            # 「账号存在 + 密码正确 + 已停用」才给停用提示，其余一律通用文案。
            # 停用提示要求调用方已证明持有密码，因此不构成账号枚举。
            raise _login_failed(matched, attrs.get("password"))

        if not self.user.is_active:
            # 防御性冗余：本项目所有认证后端都继承 ModelBackend，
            # 其 user_can_authenticate() 已把 is_active=False 过滤成 None，
            # 正常情况下走不到这里；保留以防将来接入不校验 is_active 的后端
            # （如 django.contrib.auth.backends.AllowAllUsersModelBackend）。
            raise _inactive_error()

        # 顺手记录登录 IP（放在这里，避免视图里再查一次用户）
        request = self.context.get("request")
        if request is not None:
            from .services import UserService

            UserService.record_login(self.user, _client_ip(request))

        data["user"] = UserSerializer(self.user, context=self.context).data
        return data


class LogoutSerializer(serializers.Serializer):
    """登出：把 refresh token 加入黑名单。"""

    refresh = serializers.CharField(label="refresh token")


def _inactive_error() -> BusinessError:
    """「账号已停用」的统一异常。

    用 ``BusinessError`` 而不是 DRF 的 ``AuthenticationFailed``：
    只有前者能让 ``unified_exception_handler`` 把 ``code`` 原样放进响应体
    （40101 与 ``InactiveSessionGuardMiddleware`` 同码，前端一个判断通吃两条通道）。
    """
    return BusinessError(INACTIVE_MESSAGE, code=CODE_ACCOUNT_INACTIVE, status_code=401)


def _login_failed(matched, password) -> BusinessError:
    """登录失败的统一出口（AC-26 / AC-27）。

    判定顺序：
      1. 账号存在 + 密码正确 + 已停用 → 停用提示（调用方已证明持有密码，不构成枚举）；
      2. 其余全部（账号不存在 / 密码错 / 停用了但密码也错）→ 同一句通用文案。

    ``matched`` 是登录名解析出的用户（可能为 None），由调用方在 ``authenticate()``
    之前解析好——因为认证后端会把停用账号过滤成 None，事后无法区分原因。
    """
    if matched is not None and not matched.is_active and matched.check_password(password or ""):
        return _inactive_error()
    return BusinessError(LOGIN_FAILED_MESSAGE, code=CODE_AUTH_FAILED, status_code=401)


def _client_ip(request) -> str:
    """从请求头里取真实客户端 IP（兼容反向代理）。"""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")
