"""公共模块：跨业务复用的响应封装、分页、权限、异常处理等。"""

from django.apps import AppConfig


class CommonConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.common"
    verbose_name = "公共组件"
