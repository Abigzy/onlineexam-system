"""
生产环境配置。

用法： `.env` 里设置 DJANGO_SETTINGS_MODULE=OnlineExam.settings.production
并确保 DJANGO_DEBUG=False、DJANGO_ALLOWED_HOSTS 填真实域名。
"""

from pathlib import Path

from .base import *  # noqa: F401,F403

DEBUG = False

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", [])  # noqa: F405

# ---------------- HTTPS / 安全头 ----------------
SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", True)  # noqa: F405
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_BROWSER_XSS_FILTER = True
X_FRAME_OPTIONS = "DENY"

# 前面有 Nginx 反代时，Django 只能看到 http。没有这一行，
# SECURE_SSL_REDIRECT 会把请求无限 301 到 https（ERR_TOO_MANY_REDIRECTS）。
# 要求 Nginx 侧带上 `proxy_set_header X-Forwarded-Proto $scheme;`。
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# 生产环境默认关闭跨域白名单以外的来源
CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS", [])  # noqa: F405

# ---------------- 静态文件 ----------------
# 收集目录可通过 DJANGO_STATIC_ROOT（或 STATIC_ROOT）覆盖，
# 生产环境通常指向 Nginx 可读的目录，如 /var/www/onlineexam/static。
STATIC_ROOT = Path(env("DJANGO_STATIC_ROOT", env("STATIC_ROOT", BASE_DIR / "staticfiles")))  # noqa: F405
STATICFILES_STORAGE = "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"

# ---------------- 缓存（配置 Redis 后自动生效） ----------------
REDIS_URL = env("REDIS_URL", "")  # noqa: F405
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
        }
    }

# ---------------- 日志落盘 ----------------
# 目录不存在时 RotatingFileHandler 会在 Django 启动阶段抛 FileNotFoundError，
# 整个应用起不来，所以这里主动建目录（可用 DJANGO_LOG_DIR 指到 /var/log/...）。
LOG_DIR = Path(env("DJANGO_LOG_DIR", BASE_DIR / "logs"))  # noqa: F405
LOG_DIR.mkdir(parents=True, exist_ok=True)

LOGGING["handlers"]["file"] = {  # noqa: F405
    "class": "logging.handlers.RotatingFileHandler",
    "filename": LOG_DIR / "django.log",
    "maxBytes": 10 * 1024 * 1024,
    "backupCount": 5,
    "formatter": "verbose",
}
LOGGING["root"]["handlers"] = ["console", "file"]  # noqa: F405
