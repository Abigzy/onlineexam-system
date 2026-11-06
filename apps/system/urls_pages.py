"""管理端页面路由（全部挂在 ``/manage/`` 前缀下）。

**陷阱提醒（设计文档 §2.1）**：本模块**不设 ``app_name``**。
全站的页面路由名都是扁平名字（``home`` / ``question-bank`` / ``login-page`` …），
一旦在这里设了 ``app_name = "manage"``，所有名字会被命名空间化，
模板里的 ``{% url 'manage-dashboard' %}`` 立刻 ``NoReverseMatch``，
必须写成 ``manage:manage-dashboard``——为与全站一致，保持扁平名字。

路由名约定：``manage-*``（PRD §3.1）。
"""

from django.urls import path

from apps.users import views_manage

from . import views_pages

urlpatterns = [
    # 数据总览
    path("", views_pages.ManageDashboardPageView.as_view(), name="manage-dashboard"),

    # 用户管理（实现在 apps/users，复用 UserFilter / UserService / 既有 API）
    path("users/", views_manage.ManageUserListPageView.as_view(), name="manage-user-list"),
    path("users/new/", views_manage.ManageUserCreatePageView.as_view(), name="manage-user-create"),
    path("users/<int:pk>/edit/", views_manage.ManageUserEditPageView.as_view(),
         name="manage-user-edit"),

    # 组织与基础数据 / 系统配置
    path("org/", views_pages.ManageOrgPageView.as_view(), name="manage-org"),
    path("system/", views_pages.ManageSystemPageView.as_view(), name="manage-system"),
]
