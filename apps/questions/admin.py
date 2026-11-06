"""题库后台管理。"""

from django.contrib import admin
from django.db.models import Count

from .models import KnowledgePoint, Question, QuestionOption


class QuestionOptionInline(admin.TabularInline):
    model = QuestionOption
    extra = 0
    fields = ("label", "content", "is_correct", "order")


@admin.register(KnowledgePoint)
class KnowledgePointAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "subject", "parent", "order", "is_active", "question_total")
    list_filter = ("subject", "is_active")
    search_fields = ("name", "code", "description")
    list_select_related = ("parent",)
    autocomplete_fields = ("parent",)
    ordering = ("subject", "order", "id")

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_question_total=Count("questions"))

    @admin.display(description="题目数", ordering="_question_total")
    def question_total(self, obj):
        return obj._question_total


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ("short_stem", "type", "difficulty", "score", "subject",
                    "source", "is_active", "is_reviewed", "usage_count", "created_at")
    list_filter = ("type", "difficulty", "source", "is_active", "is_reviewed", "subject")
    search_fields = ("stem", "reference_answer", "analysis")
    filter_horizontal = ("knowledge_points",)
    inlines = [QuestionOptionInline]
    date_hierarchy = "created_at"
    list_select_related = ("created_by",)
    list_per_page = 20
    readonly_fields = ("usage_count", "correct_count", "created_at", "updated_at")

    @admin.display(description="题干")
    def short_stem(self, obj):
        return obj.stem[:50]


@admin.register(QuestionOption)
class QuestionOptionAdmin(admin.ModelAdmin):
    list_display = ("question", "label", "short_content", "is_correct", "order")
    list_filter = ("is_correct",)
    search_fields = ("content",)
    autocomplete_fields = ("question",)

    @admin.display(description="内容")
    def short_content(self, obj):
        return obj.content[:40]
