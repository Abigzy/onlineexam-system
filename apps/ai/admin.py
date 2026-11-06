"""AI 调用记录后台。"""

from django.contrib import admin

from .models import AIRequestLog


@admin.register(AIRequestLog)
class AIRequestLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "provider", "scene", "success", "is_mock",
                    "total_tokens", "latency_ms", "user")
    list_filter = ("provider", "scene", "success", "is_mock")
    search_fields = ("request_preview", "response_preview", "error")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_per_page = 30
    readonly_fields = [f.name for f in AIRequestLog._meta.fields]

    def has_add_permission(self, request):
        return False  # 记账表不允许手工新增
