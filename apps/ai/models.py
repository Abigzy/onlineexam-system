"""
AI 调用记账。

每一次真实/模拟的大模型调用都落一条记录，用途：
  * 成本核算 —— 按 provider / 场景统计 token 用量；
  * 问题排查 —— 调用失败时保留错误信息与请求摘要；
  * 教学演示 —— 答辩时可以展示「AI 到底被调用了多少次、花了多少 token」。
"""

from django.conf import settings
from django.db import models


class AIRequestLog(models.Model):
    """一次大模型调用的流水记录。"""

    provider = models.CharField("平台", max_length=32, db_index=True)
    model = models.CharField("模型", max_length=64, blank=True, default="")
    scene = models.CharField("业务场景", max_length=64, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="发起人",
        null=True, blank=True, on_delete=models.SET_NULL, related_name="ai_logs",
    )

    success = models.BooleanField("是否成功", default=True, db_index=True)
    is_mock = models.BooleanField("是否本地模拟", default=False)

    prompt_tokens = models.IntegerField("输入 tokens", default=0)
    completion_tokens = models.IntegerField("输出 tokens", default=0)
    total_tokens = models.IntegerField("总 tokens", default=0)
    latency_ms = models.IntegerField("耗时(ms)", default=0)

    error = models.TextField("错误信息", blank=True, default="")
    request_preview = models.CharField("请求摘要", max_length=500, blank=True, default="")
    response_preview = models.CharField("响应摘要", max_length=500, blank=True, default="")

    created_at = models.DateTimeField("创建时间", auto_now_add=True, db_index=True)

    class Meta:
        db_table = "ai_request_log"
        verbose_name = "AI 调用记录"
        verbose_name_plural = "AI 调用记录"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["provider", "scene"], name="idx_ai_log_provider_scene"),
            models.Index(fields=["-created_at"], name="idx_ai_log_created"),
        ]

    def __str__(self):
        status = "成功" if self.success else "失败"
        return f"[{self.provider}/{self.scene}] {status} {self.total_tokens}t {self.latency_ms}ms"
