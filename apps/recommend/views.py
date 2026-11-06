"""个性化推荐接口。

路由总览（``/api/v1/`` 下）：

    GET    /recommend/students/{id}/practice/    每日练习推荐（本地理由，不调大模型）
    POST   /recommend/students/{id}/practice/    同一份推荐，但理由交给大模型改写

权限与模块五、模块六同源：

  * **跨人返回 404 而不是 403**——「这个学生 id 存不存在」不该成为可探测的信息；
  * 学生**只能看自己的**推荐；教师可以看任意学生的（用于布置针对性练习）。

为什么 GET 与 POST 分开而不是用一个 ``with_ai`` 参数：
GET 是页面打开就会发的请求，它**绝不能**顺手调大模型——学生会把页面刷新成计费器。
把「花钱的动作」单独放在 POST 上，语义更清楚，也更容易在页面上加二次确认。
"""

import logging

from django.contrib.auth import get_user_model
from django.http import Http404
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.common.constants import Role
from apps.common.response import success

from .constants import DAILY_DEFAULT_SIZE
from .serializers import PracticeQuerySerializer, PracticeReasonSerializer
from .services import RecommendationService

logger = logging.getLogger("apps.recommend.views")


def _get_student(student_id, user):
    """取学生。教师可以看任何人的；学生只能看自己，否则 404。

    顺带拦掉「target 不是学生」的情况——拿教师 / 管理员的 id 来探推荐没有意义，
    统一按「不存在」处理，避免把「这个 id 是谁」暴露出去。
    """
    student = get_object_or_404(get_user_model(), pk=student_id)
    if student.role != Role.STUDENT:
        raise Http404("学生不存在")
    if getattr(user, "is_teacher", False):
        return student
    if student.id != user.id:
        raise Http404("学生不存在")
    return student


def _practice(student, params, *, provider: str = None, user=None, with_ai: bool = False) -> dict:
    """取参数 → 成集 → （可选）AI 改写理由。"""
    data = params or {}
    subject = (data.get("subject") or "").strip()[:64]
    payload = RecommendationService.daily_practice(
        student,
        subject=subject,
        size=data.get("size") or DAILY_DEFAULT_SIZE,
        offset=data.get("offset") or 0,
    )
    if not with_ai:
        payload["ai"] = None
        return payload

    explained = RecommendationService.explain(
        student, payload["items"], subject=subject, provider=provider, user=user,
        # 措辞口径跟成集模式走：starter 的学生没有掌握度，不能说「薄弱」
        mode=payload.get("mode", "normal"),
    )
    payload["items"] = explained["items"]
    payload["ai"] = {
        "reason": explained["reason"],
        "cached": explained["cached"],
        "degraded": explained["degraded"],
        "degraded_reason": explained["degraded_reason"],
        "provider": explained["provider"],
        "model": explained["model"],
        "is_mock": explained["is_mock"],
        "source": explained["source"],
    }
    return payload


@extend_schema(tags=["个性化推荐"])
class DailyPracticeView(APIView):
    """每日练习推荐。

    * ``GET``  —— 完全确定性的一份清单，推荐理由由本地规则生成（不调大模型）；
    * ``POST`` —— 同一份清单，但推荐理由交给大模型改写（失败自动降级为本地理由）。
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="每日练习推荐（本地推荐理由）",
        parameters=[
            OpenApiParameter("subject", str, description="按学科过滤"),
            OpenApiParameter("size", int, description=f"推荐题数，默认 {DAILY_DEFAULT_SIZE}"),
            OpenApiParameter("offset", int, description="翻页偏移，「换一批」时调大"),
        ],
    )
    def get(self, request, student_id):
        student = _get_student(student_id, request.user)
        serializer = PracticeQuerySerializer(data=request.query_params or {})
        serializer.is_valid(raise_exception=True)
        payload = _practice(student, serializer.validated_data, user=request.user)
        return success(data=payload)

    @extend_schema(summary="每日练习推荐（AI 改写理由）", request=PracticeReasonSerializer)
    def post(self, request, student_id):
        student = _get_student(student_id, request.user)
        serializer = PracticeReasonSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        payload = _practice(
            student, data,
            provider=data.get("provider") or None,
            user=request.user,
            with_ai=True,
        )
        ai = payload.get("ai") or {}
        if ai.get("degraded"):
            message = "AI 暂不可用，已保留本地推荐理由"
        elif ai.get("cached"):
            message = "已返回缓存中的 AI 推荐理由"
        elif ai.get("is_mock"):
            message = "推荐理由已生成（当前使用本地模拟，未调用真实大模型）"
        else:
            message = "推荐理由已由 AI 改写完成"
        return success(data=payload, message=message)


__all__ = ["DailyPracticeView"]
