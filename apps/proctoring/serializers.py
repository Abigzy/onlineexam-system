"""作弊行为检测接口入参校验。

与模块六、模块七一致：查询参数也走 Serializer 校验。
手写 ``int(request.query_params.get(...))`` 在参数非法时会抛 500，
Serializer 会规规矩矩返回 400 并指出是哪个字段。
"""

from rest_framework import serializers


class ProctoringReportQuerySerializer(serializers.Serializer):
    """可疑考生列表的查询参数。"""

    only_suspicious = serializers.BooleanField(
        required=False, default=False, help_text="只看达到「可疑」档位以上的作答",
    )
    top_n = serializers.IntegerField(
        required=False, min_value=0, max_value=200, default=0,
        help_text="最多返回多少条，0 表示不限制",
    )


class BehaviorEventSerializer(serializers.Serializer):
    """单条行为上报（供接口文档展示，实际写库走 Service 内的宽松解析）。

    埋点解析刻意「宽进」——真实考试现场前端上报的字段可能不齐（旧版本页面、
    网络截断），这里列出的是**标准字段**，缺失的字段由 Service 兜底，
    这条纪律与「行为上报绝不能打断考试」一致。
    """

    type = serializers.CharField(max_length=32, help_text="事件类型：switch/paste/fullscreen_exit/blur")
    count = serializers.IntegerField(required=False, min_value=1, default=1)
    duration_seconds = serializers.IntegerField(required=False, min_value=0, default=0,
                                                help_text="离开/停留时长（秒）")
    question_id = serializers.IntegerField(required=False, allow_null=True)
    question_index = serializers.IntegerField(required=False, min_value=0, default=0)
    occurred_at = serializers.CharField(required=False, allow_blank=True, allow_null=True,
                                        help_text="事件时间（ISO 字符串），留空用服务端时间")
    ip_address = serializers.CharField(required=False, allow_blank=True, max_length=45)
    user_agent = serializers.CharField(required=False, allow_blank=True, max_length=255)


__all__ = ["BehaviorEventSerializer", "ProctoringReportQuerySerializer"]
