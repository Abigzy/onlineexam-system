"""大模型 Provider 实现集合。"""

import logging

from django.conf import settings

from apps.ai.exceptions import AINotConfiguredError

from .base import BaseProvider, ChatResult, build_messages, normalize_messages, system, user
from .mock import MockProvider
from .openai_compatible import OpenAICompatibleProvider

logger = logging.getLogger("apps.ai.providers")

# ------------------------------------------------------------------
# 平台注册表：新增一个平台，这里加一行就够了
# 每个平台都走 OpenAI 兼容协议，差异只在 base_url / model
# ------------------------------------------------------------------
PROVIDER_CATALOG = [
    {
        "key": "deepseek",
        "display_name": "DeepSeek（深度求索）",
        "docs": "https://platform.deepseek.com",
        "note": "中文能力强、价格低，注册即可获取 API Key",
    },
    {
        "key": "doubao",
        "display_name": "豆包（字节火山方舟）",
        "docs": "https://console.volcengine.com/ark",
        "note": "国内访问稳定，需在方舟控制台创建推理接入点",
    },
    {
        "key": "yuanbao",
        "display_name": "腾讯混元 / 元宝",
        "docs": "https://console.cloud.tencent.com/hunyuan",
        "note": "腾讯云混元大模型，OpenAI 兼容端点",
    },
    {
        "key": "openai",
        "display_name": "OpenAI（及任意兼容网关）",
        "docs": "https://platform.openai.com",
        "note": "也可指向 One-API / vLLM / Ollama 等自建兼容网关",
    },
]

_CATALOG_MAP = {item["key"]: item for item in PROVIDER_CATALOG}


def catalog() -> list:
    """返回平台清单（带当前配置状态），供前端「选择平台」下拉框使用。"""
    result = []
    for item in PROVIDER_CATALOG:
        conf = settings.AI_PROVIDERS.get(item["key"], {})
        result.append({
            **item,
            "model": conf.get("model", ""),
            "base_url": conf.get("base_url", ""),
            "configured": bool(conf.get("api_key")),
        })
    return result


def build_provider(name: str = None) -> BaseProvider:
    """按名字构造 Provider 实例。

    规则：
      * 指定了平台且该平台配了 Key → 用该平台的真实 Provider；
      * 没配 Key → 若允许 mock 则降级为 MockProvider，否则抛 AINotConfiguredError。
    """
    name = (name or settings.AI_PROVIDER or "").lower()

    if name == "mock":
        return MockProvider()

    conf = settings.AI_PROVIDERS.get(name)
    if conf is None:
        raise AINotConfiguredError(f"未知的大模型平台：{name}")

    meta = _CATALOG_MAP.get(name, {})
    provider = OpenAICompatibleProvider(
        api_key=conf.get("api_key", ""),
        base_url=conf.get("base_url", ""),
        model=conf.get("model", ""),
        timeout=settings.AI_TIMEOUT,
        max_retries=settings.AI_MAX_RETRIES,
        provider_name=name,
        display_name=meta.get("display_name", name),
    )

    if provider.is_configured:
        return provider

    if settings.AI_ALLOW_MOCK:
        logger.info("平台 %s 未配置 API Key，已降级为本地模拟", name)
        return MockProvider()

    raise AINotConfiguredError(f"平台 {name} 未配置 API Key，请在 .env 中填写后重试")


__all__ = [
    "BaseProvider",
    "ChatResult",
    "MockProvider",
    "OpenAICompatibleProvider",
    "PROVIDER_CATALOG",
    "build_messages",
    "build_provider",
    "catalog",
    "normalize_messages",
    "system",
    "user",
]
