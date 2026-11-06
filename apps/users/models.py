"""
用户模型。

设计要点：
  * 必须在第一次 migrate **之前** 定义好自定义 User（否则后期改动等于重建库）；
  * 角色用 CharField + TextChoices 而不是多张表，权限判断足够且查询简单；
  * 邮箱唯一，允许「用户名或邮箱」两种方式登录（在认证后端里实现）。
"""

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.common.constants import Role

from .managers import UserManager


class User(AbstractUser):
    """系统用户，涵盖管理员 / 教师 / 学生三种角色。"""

    # AbstractUser 自带的 first_name / last_name 在本项目里不用，统一用 real_name
    first_name = None
    last_name = None

    email = models.EmailField(_("邮箱"), unique=True)
    real_name = models.CharField(_("真实姓名"), max_length=32, blank=True, default="")
    role = models.CharField(
        _("角色"),
        max_length=16,
        choices=Role.choices,
        default=Role.STUDENT,
        db_index=True,
    )
    phone = models.CharField(_("手机号"), max_length=20, blank=True, default="")
    student_no = models.CharField(
        _("学号 / 工号"),
        max_length=32,
        blank=True,
        default="",
        db_index=True,
        help_text="学生填学号，教师填工号；唯一性在序列化器里校验",
    )
    avatar = models.ImageField(_("头像"), upload_to="avatars/%Y/%m/", blank=True, null=True)
    school = models.CharField(_("学校 / 院系"), max_length=64, blank=True, default="")
    subject = models.CharField(
        _("任教学科"),
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="教师专属：只管理该学科的资源（题库/试卷/考试）；留空表示不限学科",
    )
    last_login_ip = models.GenericIPAddressField(_("最后登录 IP"), null=True, blank=True)

    objects = UserManager()

    class Meta:
        db_table = "users"
        verbose_name = "用户"
        verbose_name_plural = "用户"
        ordering = ["-date_joined"]
        indexes = [
            models.Index(fields=["role", "is_active"], name="idx_user_role_active"),
            models.Index(fields=["-date_joined"], name="idx_user_joined"),
        ]

    def __str__(self):
        return f"{self.real_name or self.username}({self.get_role_display()})"

    @property
    def grade_year(self) -> str:
        """学生年级，从学号前 4 位解析（20260001 → "2026"）。

        刻意不单开字段：学号本来就以年级开头，再存一份会出现
        「学号 2026xxxx 但年级写成 2025」这类自相矛盾的数据。
        """
        if self.role != Role.STUDENT:
            return ""
        no = (self.student_no or "").strip()
        return no[:4] if len(no) >= 4 and no[:4].isdigit() else ""

    # ------------------------------------------------------------------
    # 便捷属性：模板里可以直接 {% if user.is_teacher %} 用
    # ------------------------------------------------------------------
    @property
    def is_admin(self) -> bool:
        return bool(self.is_superuser or self.role == Role.ADMIN)

    @property
    def is_teacher(self) -> bool:
        return bool(self.is_admin or self.role == Role.TEACHER)

    @property
    def is_student(self) -> bool:
        return self.role == Role.STUDENT

    @property
    def display_name(self) -> str:
        return self.real_name or self.username

    def get_full_name(self) -> str:
        return self.real_name or self.username
