"""AI 模块接口的序列化器。"""

from rest_framework import serializers


class ChatMessageSerializer(serializers.Serializer):
    """单条对话消息。"""

    role = serializers.ChoiceField(choices=["system", "user", "assistant"], default="user")
    content = serializers.CharField()


class AIChatRequestSerializer(serializers.Serializer):
    """调试接口入参。

    两种用法二选一：
      * 传 ``prompt`` —— 走某个业务场景的提示词模板（推荐，能验证真实链路）；
      * 传 ``messages`` —— 直接给一段对话，纯聊天调试。
    """

    prompt = serializers.CharField(required=False, allow_blank=True, help_text="用户输入内容")
    system = serializers.CharField(required=False, allow_blank=True, help_text="系统提示词")
    messages = ChatMessageSerializer(many=True, required=False, help_text="自定义消息列表")

    scene = serializers.CharField(required=False, default="generic", help_text="业务场景标识")
    variables = serializers.DictField(required=False, help_text="场景提示词模板的占位符变量")
    provider = serializers.CharField(required=False, allow_blank=True, help_text="临时指定平台")
    json_mode = serializers.BooleanField(required=False, default=False)
    temperature = serializers.FloatField(required=False, default=0.7, min_value=0.0, max_value=2.0)
    max_tokens = serializers.IntegerField(required=False, allow_null=True, min_value=1, max_value=32000)

    def validate(self, attrs):
        if not attrs.get("messages") and not attrs.get("prompt") and not attrs.get("variables"):
            raise serializers.ValidationError({"prompt": "请至少提供 prompt 或 messages"})
        return attrs


class AIChatResponseSerializer(serializers.Serializer):
    """调试接口出参（用于生成 API 文档）。"""

    content = serializers.CharField()
    provider = serializers.CharField()
    model = serializers.CharField()
    total_tokens = serializers.IntegerField()
    latency_ms = serializers.IntegerField()
    is_mock = serializers.BooleanField()
    parsed = serializers.DictField(required=False, help_text="json_mode 下解析出的结构化结果")


class AIUsageItemSerializer(serializers.Serializer):
    provider = serializers.CharField()
    scene = serializers.CharField()
    calls = serializers.IntegerField()
    success_calls = serializers.IntegerField()
    total_tokens = serializers.IntegerField()
    avg_latency_ms = serializers.IntegerField()


class ProviderInfoSerializer(serializers.Serializer):
    key = serializers.CharField()
    display_name = serializers.CharField()
    docs = serializers.CharField()
    note = serializers.CharField()
    model = serializers.CharField()
    base_url = serializers.CharField()
    configured = serializers.BooleanField()
    is_current = serializers.BooleanField()
