"""自定义认证后端：支持「用户名 / 邮箱 / 学号」任意一种登录。"""

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import Q

User = get_user_model()


class UsernameOrEmailBackend(ModelBackend):
    """页面登录（Django Session 体系）走这里。

    API 登录走 JWT（apps/users/serializers.CustomTokenObtainPairSerializer），
    但后者内部同样调用 ``authenticate()``，所以两者共用这套解析逻辑。
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None:
            username = kwargs.get(User.USERNAME_FIELD)
        if username is None or password is None:
            return None

        user = self.resolve_user(username)
        if user is None:
            # 与「密码错误」保持同一耗时特征，避免账号枚举
            User().set_password(password)
            return None

        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None

    @staticmethod
    def resolve_user(login: str):
        """把用户输入的登录名解析成 User 对象。"""
        if not login:
            return None
        login = login.strip()
        return (
            User.objects.filter(
                Q(username__iexact=login)
                | Q(email__iexact=login)
                | (Q(student_no__iexact=login) & ~Q(student_no=""))
            )
            .order_by("id")
            .first()
        )
