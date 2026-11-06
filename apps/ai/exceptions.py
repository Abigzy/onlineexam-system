"""AI 模块异常。"""


class AIServiceError(Exception):
    """调用大模型失败。

    业务层只需要捕获这一个异常类型，不用关心底层是 HTTP 错误、超时还是返回格式不对。
    """

    def __init__(self, message: str, *, provider: str = "", status_code: int = None, detail: str = ""):
        self.provider = provider
        self.status_code = status_code
        self.detail = detail
        super().__init__(message)

    def __str__(self):
        parts = [super().__str__()]
        if self.provider:
            parts.append(f"(provider={self.provider})")
        if self.status_code:
            parts.append(f"status={self.status_code}")
        if self.detail:
            parts.append(f"detail={self.detail}")
        return " ".join(parts)


class AIResponseFormatError(AIServiceError):
    """模型返回的内容不是合法的 JSON / 不符合约定结构。"""

    def __init__(self, message: str = "大模型返回内容格式不正确", *, raw: str = "", **kwargs):
        self.raw = raw
        super().__init__(message, **kwargs)


class AINotConfiguredError(AIServiceError):
    """没有配置任何可用的 provider（既没有 Key，也不允许 mock）。"""

    def __init__(self, message: str = "未配置大模型 API Key，且当前不允许使用本地模拟"):
        super().__init__(message)
