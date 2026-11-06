"""AI 适配层：把「调用大模型」这件事收进一个地方，业务代码只依赖本模块。"""

from django.apps import AppConfig


class AiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.ai"
    verbose_name = "AI 能力"
