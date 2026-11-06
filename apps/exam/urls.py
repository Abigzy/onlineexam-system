"""考试模块路由。"""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import AttemptViewSet, ExamViewSet

app_name = "exam_api"

router = DefaultRouter()
router.register("exams", ExamViewSet, basename="exam")
router.register("attempts", AttemptViewSet, basename="attempt")

urlpatterns = [
    path("", include(router.urls)),
]
