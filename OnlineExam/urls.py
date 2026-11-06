"""
项目总路由。

分层原则：
    /admin/          Django 后台
    /api/v1/...      RESTful 接口（每个业务模块一个 include）
    /api/docs/       Swagger 在线调试文档
    / ...            服务端渲染页面
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from apps.common.views import HealthView
from apps.analytics import views_pages as analytics_pages
from apps.classroom import views_pages as classroom_pages
from apps.exam import views_pages as exam_pages
from apps.grading import views_pages as grading_pages
from apps.papers import views_pages as paper_pages
from apps.proctoring import views_pages as proctoring_pages
from apps.questions import views_pages as question_pages
from apps.recommend import views_pages as recommend_pages
from apps.users import views_pages as user_pages

urlpatterns = [
    # ---------------- 后台 ----------------
    path("admin/", admin.site.urls),

    # ---------------- 页面（服务端渲染） ----------------
    path("", user_pages.HomeView.as_view(), name="home"),
    path("login/", user_pages.LoginPageView.as_view(), name="login-page"),
    path("register/", user_pages.RegisterPageView.as_view(), name="register-page"),
    path("logout/", user_pages.logout_page, name="logout-page"),
    path("profile/", user_pages.ProfilePageView.as_view(), name="profile-page"),

    # 题库模块（教师 / 管理员）
    path("question-bank/", question_pages.QuestionBankPageView.as_view(), name="question-bank"),
    path("question-bank/new/", question_pages.QuestionEditorPageView.as_view(), name="question-create"),
    path("question-bank/<int:pk>/edit/", question_pages.QuestionEditorPageView.as_view(),
         name="question-edit"),
    path("question-bank/ai-generate/", question_pages.AIGeneratePageView.as_view(), name="ai-generate"),
    path("knowledge-points/", question_pages.KnowledgePointPageView.as_view(), name="knowledge-points"),

    # 试卷与组卷模块（教师 / 管理员）
    path("papers/", paper_pages.PaperListPageView.as_view(), name="paper-list"),
    path("papers/new/", paper_pages.PaperComposePageView.as_view(), name="paper-compose-new"),
    path("papers/<int:pk>/compose/", paper_pages.PaperComposePageView.as_view(), name="paper-compose"),

    # 在线考试模块
    # 教师侧
    path("exams/", exam_pages.ExamListPageView.as_view(), name="exam-list"),
    path("exams/<int:pk>/monitor/", exam_pages.ExamMonitorPageView.as_view(), name="exam-monitor"),
    # 学生侧
    path("my-exams/", exam_pages.MyExamListPageView.as_view(), name="my-exams"),
    path("exams/<int:pk>/take/", exam_pages.ExamTakePageView.as_view(), name="exam-take"),
    path("exams/<int:pk>/result/", exam_pages.ExamResultPageView.as_view(), name="exam-result"),

    # 判分与成绩
    # 教师侧
    path("grading/", grading_pages.GradingWorkbenchPageView.as_view(), name="grading-workbench"),
    path("grading/exams/<int:pk>/scoreboard/", grading_pages.ScoreboardPageView.as_view(),
         name="grading-scoreboard"),
    # 学生侧
    path("my-wrong-book/", grading_pages.WrongBookPageView.as_view(), name="wrong-book"),

    # 学情分析
    # 教师侧
    path("analytics/", analytics_pages.AnalyticsDashboardPageView.as_view(), name="analytics-dashboard"),
    path("analytics/students/<int:pk>/", analytics_pages.StudentAnalyticsPageView.as_view(),
         name="analytics-student"),
    # 学生侧
    path("analytics/me/", analytics_pages.MyAnalyticsPageView.as_view(), name="my-analytics"),

    # 个性化推荐
    # 学生侧
    path("practice/", recommend_pages.DailyPracticePageView.as_view(), name="daily-practice"),

    # 作弊行为检测
    # 教师侧
    path("proctoring/", proctoring_pages.ProctoringDashboardPageView.as_view(),
         name="proctoring-dashboard"),
    path("proctoring/attempts/<int:pk>/evidence/",
         proctoring_pages.ProctoringEvidencePageView.as_view(), name="proctoring-evidence"),

    # 班级管理（教师 / 管理员管理，学生只读）
    path("classrooms/", classroom_pages.ClassroomListPageView.as_view(), name="classroom-list"),
    path("classrooms/<int:pk>/", classroom_pages.ClassroomDetailPageView.as_view(),
         name="classroom-detail"),

    # ---------------- 管理端（仅管理员） ----------------
    # 前缀用 /manage/ 而非 /admin/（后者已被 Django 后台占用）。
    # 页面路由名保持扁平（不加命名空间），与全站一致，见 apps/system/urls_pages.py 顶部说明。
    path("manage/", include("apps.system.urls_pages")),

    # ---------------- API v1 ----------------
    path("api/v1/", include("apps.users.urls")),
    path("api/v1/", include("apps.ai.urls")),
    path("api/v1/", include("apps.questions.urls")),
    path("api/v1/", include("apps.papers.urls")),
    path("api/v1/", include("apps.exam.urls")),
    path("api/v1/", include("apps.grading.urls")),
    path("api/v1/", include("apps.analytics.urls")),
    path("api/v1/", include("apps.recommend.urls")),
    path("api/v1/", include("apps.proctoring.urls")),
    path("api/v1/", include("apps.classroom.urls")),
    path("api/v1/", include("apps.system.urls")),

    # ---------------- 健康检查 & 接口文档 ----------------
    path("api/health/", HealthView.as_view(), name="health"),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
]

# 开发环境下由 Django 直接托管上传的用户头像等媒体文件
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

# 后台站点文案
admin.site.site_header = "AI 在线考试与学情分析系统 · 后台"
admin.site.site_title = "在线考试系统"
admin.site.index_title = "数据管理"
