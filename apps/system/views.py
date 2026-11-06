"""管理控制台的 RESTful 接口。

本模块只提供**一个**新增接口：``GET /api/v1/system/overview/``。
理由（PRD §2.5）：数据总览要跨 users / classroom / questions / papers / exam / ai
六个 app 聚合，若让前端用现有 list 接口拼装，需要 8 次以上请求、拿回整页明细、
且求和与一致性无人保证。故收成一个只读聚合接口，权限 ``IsAdmin``。
"""

from drf_spectacular.utils import extend_schema
from rest_framework.views import APIView

from apps.common.permissions import IsAdmin
from apps.common.response import success

from .services import SystemOverviewService


@extend_schema(tags=["系统"], summary="管理端数据总览（管理员）")
class SystemOverviewView(APIView):
    """GET /api/v1/system/overview/ —— 一次返回全部系统级指标。"""

    permission_classes = [IsAdmin]

    def get(self, request):
        return success(data=SystemOverviewService.overview(), message="数据总览")


__all__ = ["SystemOverviewView"]
