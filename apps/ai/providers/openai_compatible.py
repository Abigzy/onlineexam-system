"""OpenAI 兼容协议 Provider。

DeepSeek、字节豆包（火山方舟）、腾讯混元/元宝、OpenAI 官方、以及绝大多数
自建网关（One-API / New-API / vLLM / Ollama）都实现了 ``POST /chat/completions``，
所以一套代码就能全部覆盖，平台差异只在 ``base_url`` 和 ``model`` 两个配置项上。

只要对方的接口长得像这样，就能接进来：

    POST {base_url}/chat/completions
    Authorization: Bearer <api_key>
    {
        "model": "...",
        "messages": [{"role": "user", "content": "..."}],
        "temperature": 0.7
    }
"""

import json
import logging
import time

import requests

from apps.ai.exceptions import AIServiceError

from .base import BaseProvider, ChatResult, normalize_messages

logger = logging.getLogger("apps.ai.providers")

# 这些状态码值得重试（限流 / 服务端抖动）
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class OpenAICompatibleProvider(BaseProvider):
    """按 OpenAI 兼容协议发起 chat.completions 调用。"""

    name = "openai_compatible"
    display_name = "OpenAI 兼容"

    def __init__(self, *args, provider_name: str = "openai_compatible",
                 display_name: str = "OpenAI 兼容", **kwargs):
        super().__init__(*args, **kwargs)
        self.name = provider_name
        self.display_name = display_name

    # ------------------------------------------------------------------
    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _payload(self, messages, temperature, max_tokens, json_mode) -> dict:
        payload = {
            "model": self.model,
            "messages": normalize_messages(messages),
            "temperature": max(0.0, min(float(temperature), 2.0)),
        }
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        if json_mode:
            # DeepSeek / 豆包 / OpenAI 都支持；个别网关不认时会走「降级重试」
            payload["response_format"] = {"type": "json_object"}
        return payload

    # ------------------------------------------------------------------
    def chat(self, messages, *, scene="generic", temperature=0.7,
             max_tokens=None, json_mode=False) -> ChatResult:
        if not self.is_configured:
            raise AIServiceError(
                f"{self.display_name} 未配置完整（缺少 api_key / base_url / model）",
                provider=self.name,
            )

        payload = self._payload(messages, temperature, max_tokens, json_mode)
        last_error: Exception = AIServiceError("调用大模型失败", provider=self.name)
        started = self.now_ms()

        for attempt in range(self.max_retries + 1):
            try:
                response = requests.post(
                    self.endpoint,
                    headers=self._headers(),
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    timeout=self.timeout,
                )
            except requests.Timeout as exc:
                last_error = AIServiceError(f"请求超时（{self.timeout}s）", provider=self.name, detail=str(exc))
            except requests.RequestException as exc:
                last_error = AIServiceError("网络请求失败", provider=self.name, detail=str(exc))
            else:
                if response.status_code == 200:
                    return self._parse(response, started)

                detail = self._error_detail(response)

                # 网关不支持 response_format → 去掉它重试一次（不占用重试次数）
                if json_mode and response.status_code in (400, 422) and "response_format" in payload:
                    logger.info("%s 不支持 response_format，降级为纯提示词约束", self.name)
                    payload.pop("response_format", None)
                    continue

                last_error = AIServiceError(
                    self._human_message(response.status_code),
                    provider=self.name,
                    status_code=response.status_code,
                    detail=detail,
                )
                if response.status_code not in RETRYABLE_STATUS:
                    raise last_error

            # 指数退避：0.6s / 1.2s / 2.4s ...
            if attempt < self.max_retries:
                backoff = 0.6 * (2 ** attempt)
                logger.warning("%s 第 %s 次调用失败：%s，%.1fs 后重试",
                               self.name, attempt + 1, last_error, backoff)
                time.sleep(backoff)

        raise last_error

    # ------------------------------------------------------------------
    def _parse(self, response, started_ms: int) -> ChatResult:
        try:
            body = response.json()
        except ValueError as exc:
            raise AIServiceError("模型返回的不是合法 JSON", provider=self.name, detail=str(exc))

        choices = body.get("choices") or []
        if not choices:
            raise AIServiceError("模型返回中没有 choices 字段", provider=self.name,
                                 detail=json.dumps(body, ensure_ascii=False)[:500])

        message = choices[0].get("message") or {}
        # 推理模型（如 deepseek-reasoner）会把思考过程放在 reasoning_content，正文仍在 content
        content = message.get("content") or ""

        usage = body.get("usage") or {}
        return ChatResult(
            content=content,
            provider=self.name,
            model=body.get("model", self.model),
            prompt_tokens=usage.get("prompt_tokens", 0) or 0,
            completion_tokens=usage.get("completion_tokens", 0) or 0,
            total_tokens=usage.get("total_tokens", 0) or 0,
            latency_ms=self.now_ms() - started_ms,
            finish_reason=choices[0].get("finish_reason", "") or "",
            raw=body,
        )

    @staticmethod
    def _error_detail(response) -> str:
        try:
            body = response.json()
        except ValueError:
            return (response.text or "")[:500]
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            return str(error.get("message") or error)[:500]
        if error:
            return str(error)[:500]
        return json.dumps(body, ensure_ascii=False)[:500]

    @staticmethod
    def _human_message(status_code: int) -> str:
        mapping = {
            400: "请求参数有误（可能是模型名不存在或上下文超长）",
            401: "API Key 无效或已过期",
            402: "账户余额不足",
            403: "没有访问该模型的权限",
            404: "接口地址不存在，请检查 base_url",
            413: "请求内容过大",
            429: "请求过于频繁，已被限流",
        }
        if status_code >= 500:
            return "大模型服务端异常"
        return mapping.get(status_code, f"调用失败（HTTP {status_code}）")
