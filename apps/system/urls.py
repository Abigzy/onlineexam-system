"""管理控制台的 API 路由（挂在 ``/api/v1/`` 下）。

与页面路由（``urls_pages.py``）分开：
  * 这里的接口走 JWT / Session + 统一响应体 ``{code,message,data}``；
  * 页面路由走 Session，名字保持扁平（见 urls_pages.py 顶部说明）。
"""

from django.urls import path

from .views import SystemOverviewView

app_name = "system_api"

urlpatterns = [
    path("system/overview/", SystemOverviewView.as_view(), name="system-overview"),
]
