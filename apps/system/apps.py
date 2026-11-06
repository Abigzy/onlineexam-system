"""管理控制台 app。

刻意**没有 models.py**：
  * 本 app 只做「控制台外壳 + 跨模块只读聚合」，数据全部来自既有 6 个业务 app；
  * 没有模型 → 不产生任何迁移，``makemigrations --check`` 仍输出 No changes（P0 零迁移）。
"""

from django.apps import AppConfig


class SystemConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.system"
    verbose_name = "系统管理"
