"""公共视图：健康检查等与业务无关的接口。"""

from django.db import connection
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from .response import success


class HealthView(APIView):
    """GET /api/health/ —— 探活接口。

    部署后给负载均衡 / 监控系统用；也方便确认数据库连接是否正常。
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    @extend_schema(tags=["系统"], summary="健康检查")
    def get(self, request):
        db_ok = True
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception:  # 数据库不可用时不抛 500，而是明确告知
            db_ok = False

        return success(
            data={
                "status": "ok" if db_ok else "degraded",
                "database": "up" if db_ok else "down",
                "engine": connection.vendor,
            },
            message="服务运行正常" if db_ok else "数据库连接异常",
        )
