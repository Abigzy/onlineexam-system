"""
基础配置 —— 所有环境共享的部分。

约定：
  * 一切「会随环境变化 / 属于机密」的值，都从 .env 读取，不写死在代码里；
  * 开发环境差异放在 development.py，生产环境差异放在 production.py。
"""

import os
import sys
from datetime import timedelta
from pathlib import Path

import pymysql
from dotenv import load_dotenv

# ------------------------------------------------------------------
# 路径
# ------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# 让 PyMySQL 冒充 MySQLdb，免去 Windows 上编译 mysqlclient 的痛苦
pymysql.install_as_MySQLdb()

# 读取项目根目录下的 .env
load_dotenv(BASE_DIR / ".env")


def env(key: str, default=None):
    """读取环境变量；空字符串按「未设置」处理，避免 .env 里留空导致误判。"""
    value = os.environ.get(key, None)
    if value is None or value == "":
        return default
    return value


def env_bool(key: str, default: bool = False) -> bool:
    value = env(key)
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def env_list(key: str, default=None):
    """把逗号分隔的环境变量解析成列表。"""
    value = env(key)
    if value is None:
        return default or []
    return [item.strip() for item in value.split(",") if item.strip()]


# ------------------------------------------------------------------
# 安全
# ------------------------------------------------------------------
SECRET_KEY = env("DJANGO_SECRET_KEY", "django-insecure-dev-fallback-key")
DEBUG = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", ["127.0.0.1", "localhost"])

# ------------------------------------------------------------------
# 应用注册
# ------------------------------------------------------------------
DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",  # 登出时把 refresh token 拉黑
    "corsheaders",
    "django_filters",
    "drf_spectacular",
]

# 业务模块：每完成一个模块，就在这里登记一行
LOCAL_APPS = [
    "apps.common",
    "apps.users",
    "apps.ai",
    "apps.questions",
    "apps.papers",
    "apps.exam",
    "apps.grading",
    "apps.analytics",
    "apps.recommend",
    "apps.proctoring",
    "apps.classroom",
    "apps.system",  # 管理控制台外壳 + 跨模块只读聚合（无模型，零迁移）
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# ------------------------------------------------------------------
# 中间件（顺序有讲究：安全 → 跨域 → 会话 → CSRF → 认证）
# ------------------------------------------------------------------
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    # 停用账号的会话即时踢下线（AC-25）。必须排在 MessageMiddleware 之后：
    # 它要用 messages 写提示；也必须排在 AuthenticationMiddleware 之后：它要读 request.user。
    "apps.common.middleware.InactiveSessionGuardMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "OnlineExam.urls"
WSGI_APPLICATION = "OnlineExam.wsgi.application"
ASGI_APPLICATION = "OnlineExam.asgi.application"

# ------------------------------------------------------------------
# 模板
# ------------------------------------------------------------------
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# ------------------------------------------------------------------
# 数据库
# ------------------------------------------------------------------
DB_ENGINE = env("DB_ENGINE", "sqlite").lower()

if DB_ENGINE == "mysql":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": env("DB_NAME", "online_exam"),
            "USER": env("DB_USER", "root"),
            "PASSWORD": env("DB_PASSWORD", ""),
            "HOST": env("DB_HOST", "127.0.0.1"),
            "PORT": env("DB_PORT", "3306"),
            "OPTIONS": {
                # MySQL 5.6 的 InnoDB 默认单列索引上限 767 字节，
                # utf8mb4 下 VARCHAR(255) 唯一索引 = 1020 字节会建表失败。
                # 5.6 环境请把 DB_CHARSET 设为 utf8（3 字节，765 字节仍安全）。
                "charset": env("DB_CHARSET", "utf8mb4"),
                "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
            },
            # 测试库单独指定字符集：MySQL 服务端 character_set_server 若是 latin1，
            # 自动创建的 test_xxx 库会沿用 latin1，导致中文写入报 1366。
            "TEST": {
                "CHARSET": env("DB_CHARSET", "utf8mb4"),
                "COLLATION": env("DB_COLLATION", ""),
            },
        }
    }
    # Django 4.2+ 硬性要求 MySQL 8.0.11+；5.6 环境可通过开关跳过该校验
    if env_bool("DB_SKIP_VERSION_CHECK", False):
        from django.db.backends.base.base import BaseDatabaseWrapper

        BaseDatabaseWrapper.check_database_version_supported = lambda self, *a, **kw: None
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ------------------------------------------------------------------
# 认证
# ------------------------------------------------------------------
AUTH_USER_MODEL = "users.User"

# 支持「用户名 / 邮箱 / 学号」三种方式登录（页面会话与 JWT 接口共用）
AUTHENTICATION_BACKENDS = [
    "apps.users.backends.UsernameOrEmailBackend",
    "django.contrib.auth.backends.ModelBackend",  # 兜底：Django Admin 登录
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 6}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# 页面路由用的登录跳转（模板页面走 Session，API 走 JWT）
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"

# ------------------------------------------------------------------
# 国际化
# ------------------------------------------------------------------
LANGUAGE_CODE = env("DJANGO_LANGUAGE_CODE", "zh-hans")
TIME_ZONE = env("DJANGO_TIME_ZONE", "Asia/Shanghai")
USE_I18N = True
USE_TZ = True  # 库里统一存 UTC，展示层再按 TIME_ZONE 转换

# ------------------------------------------------------------------
# 静态文件 / 媒体文件
# ------------------------------------------------------------------
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# ------------------------------------------------------------------
# Django REST Framework
# ------------------------------------------------------------------
REST_FRAMEWORK = {
    # 认证：JWT 为主，Session 为辅（让 Swagger 页面也能直接调试）
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_PAGINATION_CLASS": "apps.common.pagination.StandardPagination",
    "PAGE_SIZE": 10,
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ),
    # 统一响应体：{code, message, data, ...}
    "DEFAULT_RENDERER_CLASSES": ("apps.common.renderers.UnifiedJSONRenderer",),
    "EXCEPTION_HANDLER": "apps.common.exceptions.unified_exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DATETIME_FORMAT": "%Y-%m-%d %H:%M:%S",
    # 限流（防止暴力破解登录接口）
    "DEFAULT_THROTTLE_CLASSES": ("rest_framework.throttling.ScopedRateThrottle",),
    "DEFAULT_THROTTLE_RATES": {
        "auth": "30/min",
        "ai": "20/min",
    },
}

# 跑单元测试时把限流放宽：测试会在同一个进程里反复调用登录接口，
# 走真实限流的话跑到第 30 个用例之后全都变成 429。
if "test" in sys.argv:
    REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"] = {  # noqa: F405
        "auth": "10000/min",
        "ai": "10000/min",
    }

# ------------------------------------------------------------------
# JWT
# ------------------------------------------------------------------
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=2),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
    # 自定义载荷：前端拿到 token 后可直接解析出角色，少发一次请求
    "TOKEN_OBTAIN_SERIALIZER": "apps.users.serializers.CustomTokenObtainPairSerializer",
}

# ------------------------------------------------------------------
# 跨域
# ------------------------------------------------------------------
CORS_ALLOW_ALL_ORIGINS = env_bool("CORS_ALLOW_ALL_ORIGINS", DEBUG)
CORS_ALLOW_CREDENTIALS = True
CSRF_TRUSTED_ORIGINS = env_list(
    "CSRF_TRUSTED_ORIGINS",
    ["http://127.0.0.1:8000", "http://localhost:8000"],
)

# ------------------------------------------------------------------
# OpenAPI / Swagger 文档
# ------------------------------------------------------------------
SPECTACULAR_SETTINGS = {
    "TITLE": "AI 在线考试与学情分析系统 API",
    "DESCRIPTION": (
        "基于 Django + DRF 的在线考试平台接口文档。\n\n"
        "认证方式：先调用 `/api/v1/auth/login/` 获取 `access`，"
        "再在右上角 Authorize 中填入 `Bearer <access>`。"
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "TAGS": [
        {"name": "认证", "description": "注册 / 登录 / 刷新 / 登出 / 个人资料"},
    ],
}

# ------------------------------------------------------------------
# 大模型接入（第三方 AI）
# ------------------------------------------------------------------
AI_PROVIDER = env("AI_PROVIDER", "deepseek").lower()
AI_TIMEOUT = int(env("AI_TIMEOUT", 60))
AI_MAX_RETRIES = int(env("AI_MAX_RETRIES", 2))

# 每个 provider 的接入信息；新增平台只要在这里加一段即可
AI_PROVIDERS = {
    "deepseek": {
        "api_key": env("DEEPSEEK_API_KEY", ""),
        "base_url": env("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
        "model": env("DEEPSEEK_MODEL", "deepseek-chat"),
    },
    "doubao": {
        "api_key": env("DOUBAO_API_KEY", ""),
        "base_url": env("DOUBAO_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"),
        "model": env("DOUBAO_MODEL", "doubao-pro-32k"),
    },
    "yuanbao": {
        "api_key": env("YUANBAO_API_KEY", ""),
        "base_url": env("YUANBAO_BASE_URL", "https://api.hunyuan.cloud.tencent.com/v1"),
        "model": env("YUANBAO_MODEL", "hunyuan-turbos-latest"),
    },
    "openai": {
        "api_key": env("OPENAI_API_KEY", ""),
        "base_url": env("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "model": env("OPENAI_MODEL", "gpt-4o-mini"),
    },
}

# 没有配 API Key 时是否允许「本地模拟回答」——方便离线开发和课堂演示
AI_ALLOW_MOCK = env_bool("AI_ALLOW_MOCK", True)

# 跑单元测试时一律走本地模拟：
#   * 不依赖外网 —— 断网、CI 上也能跑；
#   * 不消耗真实额度 —— 一次全量测试会触发几十次大模型调用；
#   * 结果可复现 —— 真实模型每次返回都不一样，断言没法写。
# 需要专门验证真实 provider 配置的用例，自己用 override_settings 覆盖即可。
if "test" in sys.argv:
    AI_PROVIDER = "mock"
    for _provider_conf in AI_PROVIDERS.values():
        _provider_conf["api_key"] = ""

# ------------------------------------------------------------------
# 日志
# ------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{asctime}] {levelname} {name}: {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "apps": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
