"""统一分页。

配合 ``UnifiedJSONRenderer`` 后，分页响应形如：

    {
        "code": 0,
        "message": "success",
        "data": {
            "count": 128,                      # 总条数
            "total_pages": 13,                 # 总页数
            "page": 1,                         # 当前页
            "page_size": 10,                   # 每页条数
            "next": "http://.../api/v1/xx/?page=2",
            "previous": null,
            "results": [ ... ]
        }
    }
"""

from collections import OrderedDict

from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response


class StandardPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = "page_size"  # 允许前端用 ?page_size=50 自定义
    max_page_size = 100
    page_query_param = "page"

    def paginate_queryset(self, queryset, request, view=None):
        """``page`` 取值不合法时给 **400**，而不是 DRF 默认的 404。

        ``?page=0`` / ``?page=-1`` / ``?page=abc`` 是**参数错误**，不是「资源不存在」。
        DRF 默认把这些都归成 ``InvalidPage`` → 404，在本项目里会变成 ``40400``，
        前端会误以为路径写错了（路径错和参数错该区分开）。

        注意只拦「根本不可能存在的页码」；``page=999`` 这种「格式对但超出范围」
        仍然走 DRF 的 ``EmptyPage`` → 404，那是真正的「这一页没有」。
        """
        raw = request.query_params.get(self.page_query_param)
        if raw is not None and str(raw).strip() != "":
            try:
                number = int(raw)
            except (TypeError, ValueError):
                raise ValidationError({self.page_query_param: "必须是正整数"})
            if number < 1:
                raise ValidationError({self.page_query_param: "必须从 1 开始"})
        return super().paginate_queryset(queryset, request, view)

    def get_paginated_response(self, data):
        return Response(
            OrderedDict(
                [
                    ("count", self.page.paginator.count),
                    ("total_pages", self.page.paginator.num_pages),
                    ("page", self.page.number),
                    ("page_size", self.get_page_size(self.request)),
                    ("next", self.get_next_link()),
                    ("previous", self.get_previous_link()),
                    ("results", data),
                ]
            )
        )


class LargePagination(StandardPagination):
    """需要一次性拉较多数据的场景（如题库导出、组卷选题）。"""

    page_size = 50
    max_page_size = 500
