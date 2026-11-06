"""
统一 JSON 渲染器。

作用：把 DRF 视图「顺手」返回的裸数据，统一包装成 {code, message, data}。

  * 视图里已经用了 ``ApiResponse`` / ``success()`` 的 → 原样输出（标记为 enveloped）；
  * 视图里直接 ``return Response(serializer.data)`` 的 → 自动套上外壳，code=0；
  * 出错响应 → code 取 HTTP 状态码 × 100，message 从错误体里提炼；
  * HTML 渲染（DRF 浏览器调试页）→ 不干预。
"""

import logging

from rest_framework.renderers import JSONRenderer

logger = logging.getLogger(__name__)


def _extract_message(data):
    """从各种形态的错误体里，提炼出一句能直接展示给用户的提示语。"""
    if data is None:
        return "请求失败"
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        return "; ".join(str(i) for i in data) or "请求失败"
    if isinstance(data, dict):
        # DRF 标准错误：{"detail": "..."}
        if "detail" in data:
            return _extract_message(data["detail"])
        # 字段级错误：{"username": ["该字段是必填项。"], "password": [...]}
        parts = []
        for field, value in data.items():
            text = _extract_message(value)
            parts.append(f"{field}: {text}" if field != "non_field_errors" else text)
        return "; ".join(p for p in parts if p) or "请求失败"
    return str(data)


class UnifiedJSONRenderer(JSONRenderer):
    def render(self, data, accepted_media_type=None, renderer_context=None):
        renderer_context = renderer_context or {}

        # 1) DRF 浏览器的 HTML 调试页不包装
        if accepted_media_type and "text/html" in str(accepted_media_type):
            return super().render(data, accepted_media_type, renderer_context)

        response = renderer_context.get("response")
        # 2) 已经封装过的，原样输出
        if response is not None and getattr(response, "enveloped", False):
            return super().render(data, accepted_media_type, renderer_context)

        # 3) 裸数据 → 自动套壳
        status_code = getattr(response, "status_code", 200) or 200
        if status_code >= 400:
            body = {"code": status_code * 100, "message": _extract_message(data), "data": data}
        else:
            body = {"code": 0, "message": "success", "data": data}

        return super().render(body, accepted_media_type, renderer_context)
