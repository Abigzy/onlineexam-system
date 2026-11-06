"""学情分析模块路由。

全部是「对某个资源做某事」的路径，用显式 ``path()`` 比 router 的
``@action + url_path`` 更好读（与模块五同样的取舍）。
"""

from django.urls import path

from .views import (
    AnalyticsExamListView,
    ExamAnalyticsView,
    ExamQuestionAnalyticsView,
    StudentAnalyticsView,
    StudentDiagnosisView,
    StudentMasteryView,
    StudentTrendView,
)

app_name = "analytics_api"

urlpatterns = [
    # ---- 班级 / 考试维度（教师）----
    path("analytics/exams/", AnalyticsExamListView.as_view(), name="analytics-exams"),
    path("analytics/exams/<int:exam_id>/report/", ExamAnalyticsView.as_view(),
         name="analytics-exam-report"),
    path("analytics/exams/<int:exam_id>/questions/", ExamQuestionAnalyticsView.as_view(),
         name="analytics-exam-questions"),

    # ---- 学生维度 ----
    path("analytics/students/<int:student_id>/mastery/", StudentMasteryView.as_view(),
         name="analytics-student-mastery"),
    path("analytics/students/<int:student_id>/trend/", StudentTrendView.as_view(),
         name="analytics-student-trend"),
    path("analytics/students/<int:student_id>/report/", StudentAnalyticsView.as_view(),
         name="analytics-student-report"),
    path("analytics/students/<int:student_id>/diagnosis/", StudentDiagnosisView.as_view(),
         name="analytics-student-diagnosis"),
]
