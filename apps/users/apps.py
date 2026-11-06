from django.apps import AppConfig


class UsersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.users"
    verbose_name = "用户与认证"

    def ready(self):
        """信号必须在这里导入，保证只注册一次（不要在其他地方 import signals）。"""
        from . import signals  # noqa: F401
