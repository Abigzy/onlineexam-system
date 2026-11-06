"""作弊行为检测模块路由。

与模块五、六、七同样的取舍：用显式 ``path()`` 而不是 router + ``@action``。
"""

from django.urls import path

from .views import (
    ProctoringAttemptEvidenceView,
    ProctoringExamListView,
    ProctoringExamReportView,
    ProctoringRulesView,
)

app_name = "proctoring_api"

urlpatterns = [
    path("proctoring/exams/", ProctoringExamListView.as_view(), name="proctoring-exams"),
    path("proctoring/exams/<int:exam_id>/report/", ProctoringExamReportView.as_view(),
         name="proctoring-exam-report"),
    path("proctoring/attempts/<int:attempt_id>/evidence/",
         ProctoringAttemptEvidenceView.as_view(), name="proctoring-attempt-evidence"),
    path("proctoring/rules/", ProctoringRulesView.as_view(), name="proctoring-rules"),
]
