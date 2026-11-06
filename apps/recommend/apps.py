from django.apps import AppConfig


class RecommendConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.recommend"
    verbose_name = "个性化推荐"
