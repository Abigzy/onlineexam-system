"""用户模块后台管理（Django Admin）。"""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.utils.translation import gettext_lazy as _

from .models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ("username", "real_name", "role", "student_no", "email", "is_active", "date_joined")
    list_filter = ("role", "is_active", "is_staff", "is_superuser")
    search_fields = ("username", "real_name", "email", "student_no", "phone")
    ordering = ("-date_joined",)
    list_per_page = 20

    # 注意：models.py 里把 first_name / last_name 置为 None，因此要重写 fieldsets
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        (_("个人信息"), {"fields": ("email", "real_name", "phone", "student_no", "school", "avatar")}),
        (_("权限"), {"fields": ("role", "is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        (_("重要时间"), {"fields": ("last_login", "last_login_ip", "date_joined")}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("username", "email", "role", "password1", "password2"),
        }),
    )
    readonly_fields = ("last_login", "last_login_ip", "date_joined")
