"""判分与成绩模块路由。

一部分用 DRF router（复核记录是标准的列表资源），
一部分用显式 ``path()``（``grading/attempts/{id}/grade/`` 这类「对某资源做某动作」
的路径，用 router 的 ``@action + url_path`` 反而更难读）。
"""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AnswerReviewView,
    AttemptFinalizeView,
    AttemptGradeView,
    AttemptReportView,
    CalibrationView,
    GradingExamListView,
    GradingFeedbackViewSet,
    GradingQueueView,
    ScoreboardView,
    WrongBookView,
)

app_name = "grading_api"

router = DefaultRouter()
router.register("grading/feedback", GradingFeedbackViewSet, basename="grading-feedback")

urlpatterns = [
    path("", include(router.urls)),

    # ---- 判分（教师）----
    path("grading/attempts/<int:attempt_id>/grade/", AttemptGradeView.as_view(),
         name="grading-attempt-grade"),
    path("grading/attempts/<int:attempt_id>/finalize/", AttemptFinalizeView.as_view(),
         name="grading-attempt-finalize"),
    path("grading/answers/<int:answer_id>/review/", AnswerReviewView.as_view(),
         name="grading-answer-review"),

    # ---- 批改工作台（教师）----
    path("grading/queue/", GradingQueueView.as_view(), name="grading-queue"),
    path("grading/exams/", GradingExamListView.as_view(), name="grading-exams"),

    # ---- 成绩单与统计 ----
    path("grading/attempts/<int:attempt_id>/report/", AttemptReportView.as_view(),
         name="grading-attempt-report"),
    path("grading/scoreboard/", ScoreboardView.as_view(), name="grading-scoreboard"),
    path("grading/wrong-book/", WrongBookView.as_view(), name="grading-wrong-book"),
    path("grading/calibration/", CalibrationView.as_view(), name="grading-calibration"),
]
