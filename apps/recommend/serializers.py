"""个性化推荐接口入参校验。

两类参数：

* **读接口**（GET 每日练习）—— 学科、条数、翻页偏移；
* **生成接口**（POST 生成 AI 推荐理由）—— 在前者之上多一个 ``provider``。

用 Serializer 而不是手写 ``int(request.query_params.get(...))``：
后者在 ``size=abc`` 时会抛 500，前者会规规矩矩返回 400 并指出是哪个字段。
"""

from rest_framework import serializers

from .constants import DAILY_DEFAULT_SIZE, DAILY_MAX_SIZE


class PracticeQuerySerializer(serializers.Serializer):
    """每日练习的查询参数。"""

    subject = serializers.CharField(required=False, allow_blank=True, max_length=64,
                                    help_text="按学科过滤，留空表示全部学科")
    size = serializers.IntegerField(
        required=False, min_value=1, max_value=DAILY_MAX_SIZE,
        help_text=f"推荐题数，默认 {DAILY_DEFAULT_SIZE}，最大 {DAILY_MAX_SIZE}",
    )
    offset = serializers.IntegerField(
        required=False, min_value=0,
        help_text="从第几条开始取（「换一批」就是调大 offset）",
    )


class PracticeReasonSerializer(PracticeQuerySerializer):
    """生成 AI 推荐理由的入参。

    ``provider`` 留空用系统默认；显式传 ``mock`` 可以在不联网、不花钱的前提下
    预览「AI 改写后」的样子。
    """

    provider = serializers.CharField(required=False, allow_blank=True, max_length=32,
                                     help_text="指定 AI 平台，留空用系统默认")


__all__ = [
    "PracticeQuerySerializer",
    "PracticeReasonSerializer",
]
