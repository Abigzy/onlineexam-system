"""
统一异常处理。

目标：任何一行代码抛出的异常，最终都变成同一种响应体，前端永远不用猜测格式。
同时把未预期异常记进日志，避免 500 时信息全丢。
"""

import logging

from django.http import Http404
from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.views import exception_handler as drf_exception_handler

from .response import ApiResponse

logger = logging.getLogger("apps.common.exceptions")


class BusinessError(APIException):
    """业务异常。

    在 service / view 里主动抛出，例如：
        raise BusinessError("考试已结束，无法提交", code=10010)
    """

    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "业务处理失败"
    default_code = "business_error"

    def __init__(self, detail=None, code: int = 10400, status_code: int = None):
        self.detail = detail or self.default_detail
        self.biz_code = code
        if status_code is not None:
            self.status_code = status_code
        super().__init__(self.detail)


def unified_exception_handler(exc, context):
    """替换 DRF 默认的 exception_handler（在 settings.REST_FRAMEWORK 里注册）。"""

    # 把 Django 原生的 404 / DoesNotExist 归一成 DRF 的 NotFound
    if isinstance(exc, Http404):
        exc = APIException()
        exc.status_code = status.HTTP_404_NOT_FOUND
        exc.detail = "请求的资源不存在"

    response = drf_exception_handler(exc, context)

    # 未捕获异常 → 500，并打日志
    if response is None:
        view = context.get("view")
        logger.exception("未处理异常 | view=%s | %s: %s", view.__class__.__name__ if view else "-",
                         exc.__class__.__name__, exc)
        return ApiResponse(
            data=None,
            code=50000,
            message="服务器内部错误，请稍后重试或联系管理员",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    status_code = response.status_code

    # 业务异常：直接用调用方指定的业务码
    if isinstance(exc, BusinessError):
        return ApiResponse(data=None, code=getattr(exc, "biz_code", 10400),
                           message=str(exc.detail), status_code=status_code)

    # 参数校验失败：把字段级错误放进 data，方便前端定位到具体输入框
    if isinstance(exc, ValidationError):
        return ApiResponse(
            data=_normalize(response.data),
            code=status_code * 100,
            message=_first_message(response.data),
            status_code=status_code,
        )

    # 其余（401 / 403 / 404 / 405 ...）
    return ApiResponse(
        data=None,
        code=status_code * 100,
        message=_first_message(response.data),
        status_code=status_code,
    )


def _normalize(data):
    """把 ErrorDetail 转成普通 str，避免序列化时的类型问题。"""
    if isinstance(data, dict):
        return {k: _normalize(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_normalize(v) for v in data]
    return str(data)


def _first_message(data) -> str:
    """取出第一条错误信息作为 message。"""
    if isinstance(data, dict):
        if "detail" in data:
            return _first_message(data["detail"])
        for value in data.values():
            msg = _first_message(value)
            if msg:
                return msg
        return "请求参数有误"
    if isinstance(data, list):
        return _first_message(data[0]) if data else "请求参数有误"
    return str(data) if data else "请求失败"


__all__ = ["BusinessError", "unified_exception_handler"]
