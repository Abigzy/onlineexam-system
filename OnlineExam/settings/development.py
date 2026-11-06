"""
本地开发环境配置。

用法： `.env` 里设置 DJANGO_SETTINGS_MODULE=OnlineExam.settings.development
"""

from .base import *  # noqa: F401,F403

DEBUG = True

# 开发期允许本机任意来源访问，省去反复加白名单
ALLOWED_HOSTS = ["*"]

# 允许不带尾斜杠的路径也能命中路由，方便用 Postman / curl 调试
APPEND_SLASH = True

# 开发期把邮件打到控制台，不真的发出去
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Q 集群调试时把 SQL 打出来（按需打开）
# LOGGING["loggers"]["django.db.backends"] = {
#     "handlers": ["console"],
#     "level": "DEBUG",
#     "propagate": False,
# }

# DRF 在开发环境附加浏览器调试界面。
# 注意：JSON 渲染器必须排在前面，否则接口默认会被浏览器调试页「截胡」。
# 想用调试界面时，在 URL 后面加 ?format=api 即可。
REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"] = (  # noqa: F405
    "apps.common.renderers.UnifiedJSONRenderer",
    "rest_framework.renderers.BrowsableAPIRenderer",
)
