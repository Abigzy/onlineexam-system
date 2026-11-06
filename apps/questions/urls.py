"""题库模块路由。"""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import KnowledgePointViewSet, QuestionViewSet

app_name = "questions_api"

router = DefaultRouter()
router.register("knowledge-points", KnowledgePointViewSet, basename="knowledge-point")
router.register("questions", QuestionViewSet, basename="question")

urlpatterns = [
    path("", include(router.urls)),
]
