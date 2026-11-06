"""
统一响应体。

约定：所有接口（无论成功失败）都返回同一形状，前端只需写一次解析逻辑。

    {
        "code": 0,                  # 0 = 成功；非 0 = 出错（4xxxx / 5xxxx / 1xxxx）
        "message": "success",       # 人类可读的提示语，可直接 toast 出来
        "data": { ... }             # 业务数据；出错时为字段级错误详情或 null
    }

``ApiResponse`` 带 ``enveloped = True`` 标记，渲染器看到这个标记就不再二次包装。
"""

from rest_framework import status as http_status
from rest_framework.response import Response

from .constants import API_MSG_SUCCESS, API_SUCCESS_CODE


class ApiResponse(Response):
    """已经封装好响应体的 Response。"""

    enveloped = True

    def __init__(
        self,
        data=None,
        code: int = API_SUCCESS_CODE,
        message: str = API_MSG_SUCCESS,
        status_code: int = http_status.HTTP_200_OK,
        headers=None,
        **kwargs,
    ):
        body = {"code": code, "message": message, "data": data}
        super().__init__(data=body, status=status_code, headers=headers, **kwargs)


def success(data=None, message: str = API_MSG_SUCCESS, code: int = API_SUCCESS_CODE, status_code: int = 200):
    """成功响应。"""
    return ApiResponse(data=data, code=code, message=message, status_code=status_code)


def created(data=None, message: str = "创建成功"):
    """201 创建成功。"""
    return ApiResponse(data=data, code=API_SUCCESS_CODE, message=message,
                       status_code=http_status.HTTP_201_CREATED)


def error(message: str = "请求失败", code: int = 40000, data=None, status_code: int = 400):
    """失败响应。默认按「HTTP 状态码 × 100」生成业务码。"""
    return ApiResponse(data=data, code=code, message=message, status_code=status_code)
