"""本地模拟 Provider：没有 API Key 时顶上来，保证全流程可跑通。"""

import json
import logging

from apps.ai import mocks

from .base import BaseProvider, ChatResult

logger = logging.getLogger("apps.ai.providers")


class MockProvider(BaseProvider):
    """不联网的假 Provider。

    输出严格遵循真实调用的结构约定，因此业务代码完全感知不到差别，
    切换真实 Provider 时不需要改任何业务逻辑。
    """

    name = "mock"
    display_name = "本地模拟"

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("model", "mock-model")
        kwargs.setdefault("base_url", "local://mock")
        super().__init__(*args, **kwargs)

    @property
    def is_configured(self) -> bool:
        # mock 永远可用
        return True

    def chat(self, messages, *, scene="generic", temperature=0.7,
             max_tokens=None, json_mode=False) -> ChatResult:
        started = self.now_ms()

        handler = mocks.SCENE_HANDLERS.get(scene, mocks.generic)
        try:
            payload = handler(messages)
        except Exception as exc:  # mock 自身出错也不该让业务挂掉
            logger.warning("Mock 场景 %s 处理失败：%s", scene, exc)
            payload = {"error": "mock_failed", "detail": str(exc)}

        if isinstance(payload, str):
            content = payload
        else:
            content = json.dumps(payload, ensure_ascii=False, indent=2)

        # 粗略估算 token：中文按 1 字 ≈ 1 token
        prompt_len = sum(len(str(m.get("content", ""))) for m in messages if isinstance(m, dict))
        return ChatResult(
            content=content,
            provider=self.name,
            model=self.model,
            prompt_tokens=prompt_len,
            completion_tokens=len(content),
            total_tokens=prompt_len + len(content),
            latency_ms=max(1, self.now_ms() - started),
            finish_reason="stop",
            is_mock=True,
        )
