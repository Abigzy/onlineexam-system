"""试卷模块路由。"""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import PaperViewSet

app_name = "papers_api"

router = DefaultRouter()
router.register("papers", PaperViewSet, basename="paper")

urlpatterns = [
    path("", include(router.urls)),
]
