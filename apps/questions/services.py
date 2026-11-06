"""
题库模块的业务服务层。

视图只负责收参数 / 返响应；「校验题型规则 + 落库 + 关联知识点 + 调 AI」这类
多步骤、需要事务的动作全部放在这里。
"""

import logging

from django.db import transaction

from apps.ai.client import get_ai_client
from apps.ai.exceptions import AIServiceError
from apps.ai.schemas import parse_generated_questions
from apps.common.constants import Difficulty, QuestionType
from apps.common.exceptions import BusinessError

from .constants import CHOICE_TYPES, DEFAULT_SCORE_BY_TYPE, JUDGE_OPTIONS, QuestionSource
from .models import KnowledgePoint, Question, QuestionOption

logger = logging.getLogger("apps.questions.services")


class QuestionService:
    """题目的增删改与批量操作。"""

    # ==================================================================
    # 校验
    # ==================================================================
    @staticmethod
    def validate_options(question_type: str, options: list) -> list:
        """按题型校验选项，返回清洗后的选项列表。

        规则：
          * 单选：至少 2 个选项，**有且仅有 1 个**正确
          * 多选：至少 2 个选项，至少 1 个正确
          * 判断：恰好 2 个选项（正确 / 错误），有且仅有 1 个正确
          * 填空 / 简答 / 论述：不应有选项
        """
        options = options or []

        if question_type not in CHOICE_TYPES:
            if options:
                logger.info("题型 %s 不需要选项，已忽略传入的 %s 个选项", question_type, len(options))
            return []

        if question_type == QuestionType.JUDGE:
            # 判断题统一成 A=正确 / B=错误，避免同一个题库里五花八门
            existing = {str(o.get("label", "")).upper(): o for o in options}
            normalized = []
            for idx, (label, text) in enumerate(JUDGE_OPTIONS):
                source = existing.get(label, {})
                normalized.append({
                    "label": label,
                    "content": text,  # 判断题固定文案
                    "is_correct": bool(source.get("is_correct", label == "A")),
                    "order": idx,
                })
            options = normalized

        cleaned = []
        for idx, option in enumerate(options):
            content = str(option.get("content", "")).strip()
            if not content:
                continue
            label = str(option.get("label") or chr(ord("A") + idx)).strip().upper()[:4]
            cleaned.append({
                "label": label,
                "content": content,
                "is_correct": bool(option.get("is_correct")),
                "order": idx,
            })

        # 标号去重
        seen, deduped = set(), []
        for idx, option in enumerate(cleaned):
            if option["label"] in seen:
                option["label"] = chr(ord("A") + len(seen))
            seen.add(option["label"])
            option["order"] = idx
            deduped.append(option)
        cleaned = deduped

        if len(cleaned) < 2:
            raise BusinessError(f"{QuestionType(question_type).label}至少需要 2 个选项", code=12001)

        correct_count = sum(1 for o in cleaned if o["is_correct"])

        if question_type in (QuestionType.SINGLE, QuestionType.JUDGE) and correct_count != 1:
            raise BusinessError(
                f"{QuestionType(question_type).label}必须有且仅有 1 个正确选项（当前 {correct_count} 个）",
                code=12002,
            )
        if question_type == QuestionType.MULTIPLE and correct_count < 1:
            raise BusinessError("多选题至少需要 1 个正确选项", code=12003)

        return cleaned

    @staticmethod
    def validate_answer(question_type: str, reference_answer: str):
        """非选择题必须给参考答案，否则后续没法判分。"""
        if question_type in CHOICE_TYPES:
            return
        if not (reference_answer or "").strip():
            raise BusinessError(
                f"{QuestionType(question_type).label}必须填写参考答案",
                code=12004,
            )

    # ==================================================================
    # 单条保存
    # ==================================================================
    @staticmethod
    @transaction.atomic
    def save_question(*, data: dict, user=None, instance: Question = None) -> Question:
        """新建或更新一道题（含选项与知识点关联）。"""
        options = data.pop("options", None)
        knowledge_point_ids = data.pop("knowledge_point_ids", None)
        knowledge_point_names = data.pop("knowledge_point_names", None)

        question_type = data.get("type") or (instance.type if instance else None)
        if not question_type:
            raise BusinessError("必须指定题型", code=12005)

        if instance is None:
            question = Question(**data)
            if user is not None and getattr(user, "is_authenticated", False):
                question.created_by = user
        else:
            for field, value in data.items():
                setattr(instance, field, value)
            question = instance

        question.type = question_type
        question.subject = (question.subject or "").strip()

        if options is not None or instance is None:
            cleaned_options = QuestionService.validate_options(question_type, options or [])
            # 选择题把答案冗余到 reference_answer，便于导出与快速比对
            if question_type in CHOICE_TYPES:
                question.reference_answer = "".join(
                    sorted(o["label"] for o in cleaned_options if o["is_correct"])
                )
            question._pending_options = cleaned_options  # noqa: SLF001

        if question_type not in CHOICE_TYPES:
            QuestionService.validate_answer(question_type, question.reference_answer)

        if not question.score:
            question.score = DEFAULT_SCORE_BY_TYPE.get(question_type, 2.0)

        question.save()

        # 同步选项（先删后建，简单可靠；选项数量很少，性能可忽略）
        if hasattr(question, "_pending_options"):
            question.options.all().delete()
            QuestionOption.objects.bulk_create([
                QuestionOption(question=question, **option)
                for option in question._pending_options
            ])
            del question._pending_options

        # 同步知识点：既支持传 id，也支持直接传名称（AI 出题时就只有名称）
        if knowledge_point_ids is not None or knowledge_point_names is not None:
            points = QuestionService.resolve_knowledge_points(
                ids=knowledge_point_ids, names=knowledge_point_names,
                subject=question.subject, user=user,
            )
            question.knowledge_points.set(points)

        return question

    # ==================================================================
    # 知识点
    # ==================================================================
    @staticmethod
    def resolve_knowledge_points(*, ids=None, names=None, subject: str = "", user=None) -> list:
        """把「id 列表 + 名称列表」解析成知识点对象列表。

        名称不存在时会自动创建为**该学科下的根知识点**——
        这样 AI 出题时模型随口给的知识点名不会丢失，教师后续可在知识点树里归位。
        """
        points, seen = [], set()

        if ids:
            for point in KnowledgePoint.objects.filter(id__in=ids):
                if point.id not in seen:
                    seen.add(point.id)
                    points.append(point)

        if names:
            for name in names:
                clean = str(name).strip()
                if not clean:
                    continue
                point = KnowledgePoint.objects.filter(
                    subject=subject, name=clean, parent__isnull=True
                ).first()
                if point is None:
                    point = KnowledgePoint.objects.create(
                        name=clean, subject=subject, parent=None,
                        created_by=user if getattr(user, "is_authenticated", False) else None,
                    )
                    logger.info("自动创建知识点：%s（学科=%s）", clean, subject or "-")
                if point.id not in seen:
                    seen.add(point.id)
                    points.append(point)

        return points

    # ==================================================================
    # 判重
    # ==================================================================
    @staticmethod
    def filter_duplicates(items, *, default_subject: str = "") -> tuple:
        """按「学科 + 题型 + 归一化题干」去掉重复题。

        返回 ``(unique_items, skipped)``：
          * unique_items 已移除内部 ``_row`` 键（``save_question`` 不接受未知字段）；
          * skipped = ``[{"row": 3, "stem": "...", "reason": "batch"|"db"}]``，
            reason=batch 表示同一文件内重复，db 表示题库里已有。

        判重规则：
          * 文件内先自查重，同一批里只保留第一条；
          * 再与库内比较，**不过滤 is_active** —— 已停用的题也算「已存在」，
            否则导入会把停用过的题又原样搬回题库；
          * 一个 item 同时命中两者时，以 batch 为准（它连库都没进）；
          * 不同学科、不同题型的同题干不算重复。
        """
        from .importers import normalize_stem, normalize_subject

        unique_items, skipped = [], []
        seen, rows = set(), {}  # rows：id(payload) -> 行号，供库内查重时回报

        for item in items or []:
            payload = dict(item)
            row = payload.pop("_row", None)
            key = (
                normalize_subject(payload.get("subject") or default_subject),
                payload.get("type"),
                normalize_stem(payload.get("stem")),
            )
            if key in seen:
                skipped.append({"row": row, "stem": payload.get("stem", ""), "reason": "batch"})
                continue
            seen.add(key)
            rows[id(payload)] = row
            unique_items.append(payload)

        if not unique_items:
            return [], skipped

        # 库内查重：一次查询拉候选，避免逐条 exists 打穿数据库
        subjects = {item.get("subject") or default_subject for item in unique_items}
        existing = {
            (
                normalize_subject(subject),
                question_type,
                normalize_stem(stem),
            )
            for subject, question_type, stem in Question.objects.filter(
                subject__in=subjects
            ).values_list("subject", "type", "stem")
        }

        kept = []
        for item in unique_items:
            key = (
                normalize_subject(item.get("subject") or default_subject),
                item.get("type"),
                normalize_stem(item.get("stem")),
            )
            if key in existing:
                skipped.append({
                    "row": rows.get(id(item)),
                    "stem": item.get("stem", ""),
                    "reason": "db",
                })
                continue
            kept.append(item)

        # 按行号排序，教师看跳过明细时顺序与文件一致（无行号的排最后）
        skipped.sort(key=lambda entry: (entry["row"] is None, entry["row"] or 0))
        return kept, skipped

    # ==================================================================
    # 批量
    # ==================================================================
    @staticmethod
    @transaction.atomic
    def bulk_create(*, items: list, user=None, default_subject: str = "") -> list:
        """批量入库（AI 出题确认后、或导入 Excel 时用）。

        返回创建成功的题目列表；单条失败只跳过该条并记录，不影响其余题目。
        """
        created, errors = [], []
        for index, item in enumerate(items):
            payload = dict(item)
            payload.setdefault("source", QuestionSource.AI)
            payload.setdefault("subject", default_subject)
            try:
                created.append(QuestionService.save_question(data=payload, user=user))
            except (BusinessError, ValueError, TypeError) as exc:
                errors.append({"index": index, "error": str(exc)})
                logger.warning("批量建题第 %s 条失败：%s", index, exc)

        if not created:
            detail = errors[0]["error"] if errors else "没有可入库的题目"
            raise BusinessError(f"批量入库失败：{detail}", code=12006)

        return created, errors

    # ==================================================================
    # 删除
    # ==================================================================
    @staticmethod
    @transaction.atomic
    def soft_delete(question: Question) -> None:
        """软删除：题目可能已被试卷引用，物理删除会破坏历史数据。

        后续模块（试卷、成绩）若已产生作答记录，只能停用不能删除。
        """
        question.is_active = False
        question.save(update_fields=["is_active", "updated_at"])


class QuestionAIService:
    """基于大模型的出题与解题服务。"""

    @staticmethod
    def generate_drafts(*, user, subject: str = "", knowledge_points=None,
                        question_types=None, difficulty: int = Difficulty.MEDIUM,
                        count: int = 5, source_text: str = "", provider: str = None) -> dict:
        """调用大模型生成题目草稿（**不落库**）。

        返回 ``{"drafts": [...], "meta": {...}}``；教师在前端确认/编辑后再调用
        ``POST /api/v1/questions/batch/`` 入库。
        """
        knowledge_points = [k for k in (knowledge_points or []) if str(k).strip()]
        question_types = question_types or [QuestionType.SINGLE]

        type_labels = "、".join(QuestionType(t).label for t in question_types if t in dict(QuestionType.choices))
        difficulty_labels = {
            Difficulty.EASY: "简单（1 星）",
            Difficulty.MEDIUM: "中等（2 星）",
            Difficulty.HARD: "困难（3 星）",
            Difficulty.EXPERT: "挑战（4 星）",
        }
        difficulty_desc = difficulty_labels.get(int(difficulty), "中等")

        client = get_ai_client(provider)
        try:
            result = client.run_scene(
                "generate_questions",
                user=user,
                subject=subject or "（未指定）",
                knowledge_points="、".join(knowledge_points) or "（未指定，请围绕素材自行提炼）",
                question_types=type_labels or "单选题",
                difficulty_desc=difficulty_desc,
                count=count,
                source_text=(source_text or "（未提供素材，请按知识点自行命制）")[:4000],
            )
        except AIServiceError as exc:
            raise BusinessError(f"AI 出题失败：{exc}", code=12010, status_code=502)

        try:
            generated = parse_generated_questions(result.json(strict=True), max_items=count)
        except AIServiceError as exc:
            raise BusinessError(f"AI 返回内容无法解析为题目：{exc}", code=12011, status_code=502)

        drafts = []
        for index, item in enumerate(generated):
            payload = item.to_dict()
            payload["index"] = index
            # 知识点名称交给前端展示，入库时由 resolve_knowledge_points 落成正式关联
            payload["knowledge_point_names"] = payload.pop("knowledge_points", [])
            if not payload["knowledge_point_names"]:
                payload["knowledge_point_names"] = knowledge_points[:1]
            drafts.append(payload)

        return {
            "drafts": drafts,
            "meta": {
                "requested": count,
                "generated": len(drafts),
                "subject": subject,
                "question_types": question_types,
                "difficulty": int(difficulty),
                "provider": client.name,
                "model": client.provider.model,
                "is_mock": client.is_mock,
                "total_tokens": result.total_tokens,
                "latency_ms": result.latency_ms,
            },
        }

    @staticmethod
    def explain_question(*, question: Question, user=None, provider: str = None) -> str:
        """让 AI 为一道题撰写解析。"""
        options_text = "\n".join(
            f"{option.label}. {option.content}" for option in question.options.all()
        ) or "（无选项）"
        knowledge_names = "、".join(kp.name for kp in question.knowledge_points.all()) or "（未标注）"

        client = get_ai_client(provider)
        try:
            result = client.run_scene(
                "explain_question",
                user=user,
                json_mode=False,
                question_type=question.get_type_display(),
                stem=question.stem,
                options=options_text,
                reference_answer=question.correct_answer_text() or "（未提供）",
                knowledge_points=knowledge_names,
            )
        except AIServiceError as exc:
            raise BusinessError(f"AI 生成解析失败：{exc}", code=12012, status_code=502)

        return result.content.strip()

    @staticmethod
    def grade_subjective(*, question: Question, student_answer: str, user=None,
                         provider: str = None) -> dict:
        """主观题评分。模块五（判分）会正式接入，这里先提供能力。

        返回 ``{"score_ratio": 0.75, "score": 7.5, ...}``。
        """
        from apps.ai.schemas import parse_grading_result, score_by_threshold

        full_score = float(question.score or 5)
        client = get_ai_client(provider)
        try:
            result = client.run_scene(
                "grade_subjective",
                user=user,
                stem=question.stem,
                full_score=full_score,
                reference_answer=question.reference_answer or "（未提供参考答案）",
                student_answer=student_answer or "（未作答）",
            )
        except AIServiceError as exc:
            raise BusinessError(f"AI 评分失败：{exc}", code=12013, status_code=502)

        try:
            grading = parse_grading_result(result.json(strict=True))
        except AIServiceError as exc:
            raise BusinessError(f"AI 评分结果无法解析：{exc}", code=12014, status_code=502)

        payload = grading.to_dict()
        payload["score"] = score_by_threshold(grading.score_ratio, full_score=full_score)
        payload["full_score"] = full_score
        payload["is_mock"] = client.is_mock
        return payload
