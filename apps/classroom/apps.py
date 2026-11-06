"""班级模块应用配置。"""

from django.apps import AppConfig


class ClassroomConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.classroom"
    verbose_name = "班级管理"
