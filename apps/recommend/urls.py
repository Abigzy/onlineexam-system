"""个性化推荐模块路由。

与模块五、模块六同样的取舍：用显式 ``path()`` 而不是 router + ``@action``，
「对某个资源做某事」的语义更直白。
"""

from django.urls import path

from .views import DailyPracticeView

app_name = "recommend_api"

urlpatterns = [
    # 每日练习：GET 取本地推荐，POST 让大模型改写推荐理由
    path("recommend/students/<int:student_id>/practice/", DailyPracticeView.as_view(),
         name="recommend-student-practice"),
]
