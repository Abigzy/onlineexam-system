"""学情分析接口入参校验。

学情分析的读接口大量使用查询参数（学科筛选、趋势条数上限），
把这些参数的校验集中在这里，视图只负责「取参数 → 调 Service → 返回」。

**为什么用 Serializer 校验查询参数**：手写 ``request.query_params.get(...)`` +
``int()`` 的写法在参数非法时会抛 500（比如 ``limit=abc``），
而 Serializer 会规规矩矩返回 400 并指出是哪个字段。
"""

from rest_framework import serializers

from .constants import TREND_DEFAULT_LIMIT, TREND_MAX_LIMIT


class AnalyticsQuerySerializer(serializers.Serializer):
    """读接口的通用查询参数。"""

    subject = serializers.CharField(required=False, allow_blank=True, max_length=64,
                                    help_text="按学科过滤，留空表示全部学科")
    limit = serializers.IntegerField(
        required=False, min_value=1, max_value=TREND_MAX_LIMIT,
        help_text=f"条数上限，默认 {TREND_DEFAULT_LIMIT}，最大 {TREND_MAX_LIMIT}",
    )


class ExamReportQuerySerializer(serializers.Serializer):
    """班级学情报告的查询参数。"""

    include_rows = serializers.BooleanField(
        required=False, default=False,
        help_text="是否附带逐人明细（成绩单已有一份，默认不带，减小响应体）",
    )
    classroom_id = serializers.IntegerField(
        required=False, allow_null=True,
        help_text="按班级过滤（传了只统计该班学生，不传统计整场考试）",
    )


class DiagnosisRequestSerializer(serializers.Serializer):
    """生成学情诊断的入参。

    ``force=False`` 且缓存未过期时**不会重新调用大模型**——
    这是花钱的动作，不该因为前端多刷一次页面就重复计费。
    """

    subject = serializers.CharField(required=False, allow_blank=True, max_length=64)
    force = serializers.BooleanField(required=False, default=False,
                                     help_text="忽略缓存强制重新生成")
    provider = serializers.CharField(required=False, allow_blank=True, max_length=32,
                                     help_text="指定 AI 平台，留空用系统默认")


__all__ = [
    "AnalyticsQuerySerializer",
    "DiagnosisRequestSerializer",
    "ExamReportQuerySerializer",
]
