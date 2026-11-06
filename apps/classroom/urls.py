"""班级模块 API 路由。

与模块五 / 六 / 七 / 八同样的取舍：用显式 ``path()`` 而不是 router + ``@action``。
"""

from django.urls import path

from .views import (
    ClassroomDetailView,
    ClassroomListView,
    ClassroomMembersView,
    ClassroomOptionsView,
)

app_name = "classroom_api"

urlpatterns = [
    path("classrooms/", ClassroomListView.as_view(), name="classroom-list"),
    path("classrooms/options/", ClassroomOptionsView.as_view(), name="classroom-options"),
    path("classrooms/<int:classroom_id>/", ClassroomDetailView.as_view(), name="classroom-detail"),
    path("classrooms/<int:classroom_id>/students/", ClassroomMembersView.as_view(),
         name="classroom-members"),
]
