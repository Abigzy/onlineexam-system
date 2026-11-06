"""AI 模块路由。"""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import AIViewSet

app_name = "ai_api"

router = DefaultRouter()
router.register("", AIViewSet, basename="ai")

urlpatterns = [
    path("ai/", include(router.urls)),
]
