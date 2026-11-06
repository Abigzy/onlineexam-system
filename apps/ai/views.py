"""
AI 模块接口。

路由：
    POST   /api/v1/ai/chat/              在线调试（走真实 provider 或 mock）
    POST   /api/v1/ai/test-connection/   测试当前平台连通性
    GET    /api/v1/ai/providers/         平台清单与配置状态
    GET    /api/v1/ai/usage/             调用用量统计
    GET    /api/v1/ai/logs/              最近调用记录

权限：教师 / 管理员。这是「教学与运维」接口，学生不需要访问。
"""

import logging

from django.conf import settings
from django.db.models import Avg, Count, Q, Sum
from drf_spectacular.utils import extend_schema
from rest_framework import viewsets
from rest_framework.decorators import action

from apps.common.exceptions import BusinessError
from apps.common.pagination import StandardPagination
from apps.common.permissions import IsTeacher
from apps.common.response import success

from .client import get_ai_client
from .exceptions import AIServiceError
from .models import AIRequestLog
from .prompts import available_prompts
from .providers import build_messages, catalog
from .serializers import AIChatRequestSerializer

logger = logging.getLogger("apps.ai.views")


@extend_schema(tags=["AI 能力"])
class AIViewSet(viewsets.GenericViewSet):
    """大模型能力的统一入口。"""

    permission_classes = [IsTeacher]
    serializer_class = AIChatRequestSerializer
    pagination_class = StandardPagination
    throttle_scope = "ai"

    # ------------------------------------------------------------------
    @extend_schema(summary="在线调试大模型", request=AIChatRequestSerializer)
    @action(detail=False, methods=["post"], url_path="chat")
    def chat(self, request):
        serializer = AIChatRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        client = get_ai_client(data.get("provider") or None)
        messages_sent = 2

        try:
            if data.get("messages"):
                messages = data["messages"]
                messages_sent = len(messages)
                result = client.chat(
                    messages,
                    scene=data.get("scene") or "generic",
                    user=request.user,
                    temperature=data.get("temperature", 0.7),
                    max_tokens=data.get("max_tokens"),
                    json_mode=data.get("json_mode", False),
                )
            elif data.get("variables"):
                # 走场景模板（只填变量，不用手写提示词）
                messages_sent = 2
                result = client.run_scene(
                    data.get("scene") or "generic",
                    user=request.user,
                    json_mode=data.get("json_mode", False),
                    temperature=data.get("temperature", 0.7),
                    max_tokens=data.get("max_tokens"),
                    **data["variables"],
                )
            else:
                result = client.chat(
                    build_messages(data.get("system", ""), data["prompt"]),
                    scene=data.get("scene") or "generic",
                    user=request.user,
                    temperature=data.get("temperature", 0.7),
                    max_tokens=data.get("max_tokens"),
                    json_mode=data.get("json_mode", False),
                )
        except AIServiceError as exc:
            raise BusinessError(str(exc), code=11001, status_code=502)

        payload = result.to_dict()
        payload["messages_sent"] = messages_sent

        # json_mode 下顺手尝试解析，解析失败不算请求失败（可能只是模板用错了）
        if data.get("json_mode"):
            try:
                payload["parsed"] = result.json(strict=True)
            except AIServiceError as exc:
                payload["parsed"] = None
                payload["parse_error"] = str(exc)

        return success(data=payload, message="调用成功（本地模拟）" if result.is_mock else "调用成功")

    # ------------------------------------------------------------------
    @extend_schema(summary="测试平台连通性")
    @action(detail=False, methods=["post"], url_path="test-connection")
    def test_connection(self, request):
        client = get_ai_client(request.data.get("provider") or None)
        try:
            result = client.check_connection()
        except AIServiceError as exc:
            return success(
                data={"ok": False, "provider": client.name, "error": str(exc)},
                message=f"连接失败：{exc}",
            )
        return success(
            data={
                "ok": True,
                **client.info(),
                "reply": result.content.strip()[:100],
                "latency_ms": result.latency_ms,
            },
            message="本地模拟模式，未真正联网" if result.is_mock else "连接正常",
        )

    # ------------------------------------------------------------------
    @extend_schema(summary="平台清单与配置状态")
    @action(detail=False, methods=["get"], url_path="providers")
    def providers(self, request):
        current = (settings.AI_PROVIDER or "").lower()
        items = catalog()
        for item in items:
            item["is_current"] = item["key"] == current
        return success(data={
            "current": current,
            "allow_mock": settings.AI_ALLOW_MOCK,
            "active_is_mock": get_ai_client().is_mock,
            "providers": items,
            "prompts": available_prompts(),
        })

    # ------------------------------------------------------------------
    @extend_schema(summary="调用用量统计")
    @action(detail=False, methods=["get"], url_path="usage")
    def usage(self, request):
        queryset = AIRequestLog.objects.all()
        days = request.query_params.get("days")
        if days and str(days).isdigit():
            from django.utils import timezone
            from datetime import timedelta

            queryset = queryset.filter(created_at__gte=timezone.now() - timedelta(days=int(days)))

        by_scene = list(
            queryset.values("scene")
            .annotate(
                calls=Count("id"),
                success_calls=Count("id", filter=Q(success=True)),
                total_tokens=Sum("total_tokens"),
                avg_latency_ms=Avg("latency_ms"),
            )
            .order_by("-calls")
        )
        for row in by_scene:
            row["avg_latency_ms"] = int(row["avg_latency_ms"] or 0)
            row["total_tokens"] = row["total_tokens"] or 0

        totals = queryset.aggregate(
            calls=Count("id"),
            success_calls=Count("id", filter=Q(success=True)),
            mock_calls=Count("id", filter=Q(is_mock=True)),
            total_tokens=Sum("total_tokens"),
            avg_latency_ms=Avg("latency_ms"),
        )
        totals["total_tokens"] = totals["total_tokens"] or 0
        totals["avg_latency_ms"] = int(totals["avg_latency_ms"] or 0)

        return success(data={"totals": totals, "by_scene": by_scene})

    # ------------------------------------------------------------------
    @extend_schema(summary="最近调用记录")
    @action(detail=False, methods=["get"], url_path="logs")
    def logs(self, request):
        queryset = AIRequestLog.objects.all()
        if request.query_params.get("scene"):
            queryset = queryset.filter(scene=request.query_params["scene"])
        if request.query_params.get("success") in ("true", "false"):
            queryset = queryset.filter(success=request.query_params["success"] == "true")

        page = self.paginate_queryset(queryset)
        data = [
            {
                "id": log.id,
                "provider": log.provider,
                "model": log.model,
                "scene": log.scene,
                "success": log.success,
                "is_mock": log.is_mock,
                "total_tokens": log.total_tokens,
                "latency_ms": log.latency_ms,
                "error": log.error[:200],
                "user": log.user.username if log.user else "",
                "created_at": log.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            }
            for log in page
        ]
        return self.get_paginated_response(data)
