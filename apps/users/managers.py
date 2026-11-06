"""User 模型的查询集与管理器。

把「常用筛选」收进 QuerySet，视图里只写业务语义，不写 ORM 细节。
"""

from django.contrib.auth.models import UserManager as DjangoUserManager
from django.db import models


class UserQuerySet(models.QuerySet):
    """用户查询集。"""

    def active(self):
        return self.filter(is_active=True)

    def students(self):
        return self.filter(role="student")

    def teachers(self):
        return self.filter(role="teacher")

    def admins(self):
        return self.filter(models.Q(role="admin") | models.Q(is_superuser=True))

    def search(self, keyword: str):
        """按用户名 / 姓名 / 学号 / 邮箱模糊搜索。"""
        if not keyword:
            return self
        return self.filter(
            models.Q(username__icontains=keyword)
            | models.Q(real_name__icontains=keyword)
            | models.Q(student_no__icontains=keyword)
            | models.Q(email__icontains=keyword)
        )

    def of_role(self, role: str):
        return self.filter(role=role) if role else self

    def order_by_created(self, desc: bool = True):
        return self.order_by("-date_joined" if desc else "date_joined")


class UserManager(DjangoUserManager.from_queryset(UserQuerySet)):
    """保留 Django 默认的 create_user / create_superuser 能力，同时获得自定义查询方法。"""

    def create_superuser(self, username, email=None, password=None, **extra_fields):
        """超级用户默认角色设为管理员。"""
        extra_fields.setdefault("role", "admin")
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        return super().create_superuser(username, email, password, **extra_fields)
