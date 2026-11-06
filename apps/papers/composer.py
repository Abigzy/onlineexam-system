"""智能组卷算法。

设计取向（先说清楚，免得后面有人问「为什么不用 XXX 算法」）：

  这是一个**带约束的选题问题**：在「题型配额 + 难度配比 + 知识点覆盖 + 不重复」
  的约束下挑出一组题，同时让整体难度逼近目标。把它当成最优化问题精确求解是
  NP 难的（多约束指派），而且教学场景根本不需要最优解——教师看完还要手工微调。
  所以采用**分层贪心**：

    1. 按目标难度插值出难度配比（目标 2.0 → 简单 20% / 中等 60% / 困难 20%）；
    2. 每个题型按配额逐档抽题；
    3. 抽题时做**知识点轮转**——优先选本卷刚好用得最少的知识点，让覆盖自动摊平；
    4. 某个难度档库存不足时，向相邻难度回退补位，并在报告里如实标注「不足」。

  第 3 条是让卷子「看起来像样」的关键：纯粹随机会把 10 道单选全砸在两三个
  知识点上，教师一眼就看出是机器出的。

结果的可解释性比「数学上更优」更重要，所以 ``compose`` 会返回一份**组卷报告**，
明确告诉教师哪些题是降级补位的、知识点覆盖了几个、实际难度与目标差多少。
"""

import logging
import random
import time
from decimal import Decimal

from django.db import transaction

from apps.common.constants import Difficulty, QuestionType
from apps.common.exceptions import BusinessError
from apps.questions.models import Question

from .constants import (
    RANDOM_SCORE_BY_TYPE,
    PaperGenerationMode,
    SECTION_TITLE_BY_TYPE,
    SECTION_TYPE_ORDER,
)
from .models import Paper
from .services import PaperService

logger = logging.getLogger("apps.papers.composer")

DIFFICULTY_LEVELS = [int(value) for value, _ in Difficulty.choices]

# 默认蓝图：一份 100 分的常规卷子
DEFAULT_BLUEPRINT = [
    {"type": QuestionType.SINGLE, "count": 10, "score": 2},    # 20 分
    {"type": QuestionType.MULTIPLE, "count": 5, "score": 3},   # 15 分
    {"type": QuestionType.JUDGE, "count": 10, "score": 1},     # 10 分
    {"type": QuestionType.FILL, "count": 5, "score": 2},       # 10 分
    {"type": QuestionType.SHORT, "count": 3, "score": 10},     # 30 分
    {"type": QuestionType.ESSAY, "count": 1, "score": 15},     # 15 分
]

# 难度偏差超过这个值就认为没配准（与 AnalysisThreshold 保持同一量级）
DIFFICULTY_TOLERANCE = 0.35


class PaperComposeService:
    """按约束自动组卷。"""

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    @staticmethod
    def compose(*, user=None, subject: str = "", knowledge_point_ids=None,
                blueprint: list = None, target_difficulty: float = 2.0,
                difficulty_mix: dict = None, exclude_ids=None,
                title: str = "", duration_minutes: int = 90,
                pass_score=None, seed: int = None,
                generation_mode=PaperGenerationMode.AI,
                title_label: str = "智能组卷") -> dict:
        """组一份卷子并落库，返回 ``{"paper": Paper, "report": {...}}``。"""
        started = time.time()
        plan = PaperComposeService.preview(
            subject=subject, knowledge_point_ids=knowledge_point_ids, blueprint=blueprint,
            target_difficulty=target_difficulty, difficulty_mix=difficulty_mix,
            exclude_ids=exclude_ids, seed=seed,
        )
        selected = plan["selected"]

        if not selected:
            raise BusinessError(
                "按当前条件没有抽到任何题目，请放宽知识点范围或降低题量要求", code=13010,
            )

        with transaction.atomic():
            paper = Paper.objects.create(
                title=title or PaperComposeService._default_title(subject, plan, label=title_label),
                subject=subject,
                description=plan["report"]["summary"],
                duration_minutes=duration_minutes,
                pass_score=(
                    Decimal(str(pass_score))
                    if pass_score is not None
                    else (plan["total_score"] * Decimal("0.6")).quantize(Decimal("0.01"))
                ),
                target_difficulty=float(target_difficulty),
                generation_mode=generation_mode,
                created_by=user if getattr(user, "is_authenticated", False) else None,
            )

            # 按题型分批入库：分批的意义在于每批能带自己的「每题分值」，
            # 落库后由 sync_sections 自动归到对应大题里。
            for row in plan["quota_table"]:
                items = plan["picked"][row["type"]]
                if not items:
                    continue
                PaperService.add_questions(
                    paper=paper,
                    questions=[item["question"] for item in items],
                    score=row["score"],
                    strict=False,
                )

            PaperService.recalc(paper)

        report = plan["report"]
        report["paper_id"] = paper.id
        report["title"] = paper.title
        report["total_score"] = float(paper.total_score)
        report["question_count"] = paper.question_count
        report["actual_difficulty"] = paper.actual_difficulty()
        report["difficulty_deviation"] = paper.difficulty_deviation()
        report["elapsed_ms"] = int((time.time() - started) * 1000)
        report["seed"] = seed

        mode_label = PaperGenerationMode(generation_mode).label
        logger.info(
            "%s「%s」：%s 题 / %s 分 / 实际难度 %s（目标 %s）",
            mode_label, paper.title, paper.question_count, paper.total_score,
            report["actual_difficulty"], target_difficulty,
        )
        return {"paper": paper, "report": report}

    # ------------------------------------------------------------------
    # 试算（不落库）
    # ------------------------------------------------------------------
    @staticmethod
    def preview(*, subject: str = "", knowledge_point_ids=None, blueprint: list = None,
                target_difficulty: float = 2.0, difficulty_mix: dict = None,
                exclude_ids=None, seed: int = None) -> dict:
        """只算不写。

        教师点「智能组卷」时先跑这个：题库够不够、能覆盖几个知识点、
        大概是什么难度，全都先摆出来，再决定要不要真的生成。
        """
        started = time.time()
        rows = PaperComposeService._normalize_blueprint(blueprint)
        mix = difficulty_mix or PaperComposeService.plan_difficulty_mix(target_difficulty)
        pool = PaperComposeService.build_pool(
            subject=subject, knowledge_point_ids=knowledge_point_ids, exclude_ids=exclude_ids,
        )

        rng = random.Random(seed)
        kp_usage = {}
        picked, quota_table, shortage, adjusted = {}, [], [], []
        locked_ids = set()

        for row in rows:
            code = row["type"]
            candidates = pool.get(code, [])
            # 先按理想配比切分，再按题库真实库存校正——判断题一道难题都没有时，
            # 提前把这部分配额转给邻近难度，比留到最后靠回退乱补要准得多。
            quota = PaperComposeService._rebalance_quota(
                PaperComposeService._split_by_mix(row["count"], mix),
                candidates, target_difficulty,
            )

            chosen = []
            for level in sorted(quota, key=lambda x: (abs(x - target_difficulty), x)):
                need = quota[level]
                if need <= 0:
                    continue
                chosen.extend(PaperComposeService._take(
                    candidates=candidates, level=level, need=need,
                    kp_usage=kp_usage, rng=rng, locked_ids=locked_ids,
                ))
            # 某档库存不够 → 向相邻难度回退补齐。这不算「组不出来」，
            # 只是难度配比打了点折扣，单独记进 adjusted 让教师知情，
            # 不要和真的「题不够」混在一个列表里。
            gap = row["count"] - len(chosen)
            if gap > 0:
                fallback = PaperComposeService._take(
                    candidates=candidates, level=None, need=gap,
                    kp_usage=kp_usage, rng=rng, locked_ids=locked_ids,
                    target=target_difficulty,
                )
                chosen.extend(fallback)
                if fallback:
                    adjusted.append({
                        "type": code,
                        "label": row["label"],
                        "count": len(fallback),
                        "message": (
                            f"{row['label']}按难度配比没抽满，已用其他难度的题目补了 "
                            f"{len(fallback)} 道，实际难度会略有偏移"
                        ),
                    })

            # 分值以**蓝图**为准。卷面上「单项选择题每题 2 分」是教师定的规则，
            # 不该因为题库里某道题当初被设成 3 分就改变卷面结构。
            for item in chosen:
                item["score"] = row["score"]

            if len(chosen) < row["count"]:
                shortage.append({
                    "type": code,
                    "label": row["label"],
                    "requested": row["count"],
                    "filled": len(chosen),
                    "message": (
                        f"{row['label']}需要 {row['count']} 道，"
                        f"但题库里符合条件的只有 {len(candidates)} 道，"
                        f"缺 {row['count'] - len(chosen)} 道"
                    ),
                })

            picked[code] = chosen
            quota_table.append({
                "type": code,
                "label": row["label"],
                "count": len(chosen),
                "requested": row["count"],
                "score": row["score"],
                "subtotal": float(Decimal(str(row["score"])) * len(chosen)),
                "available": len(candidates),
            })

        flat = [item for items in picked.values() for item in items]
        # 初值必须显式给 Decimal：空列表时 sum([]) 返回的是 int 0，
        # 后面 .quantize() 会直接抛 AttributeError（题库为空时就会踩到）。
        total_score = sum((Decimal(str(item["score"])) for item in flat), Decimal("0"))
        actual_mix = PaperComposeService._mix_of(flat)

        report = {
            "summary": PaperComposeService._summary(rows, flat, total_score, target_difficulty),
            "requested": {
                "subject": subject,
                "target_difficulty": float(target_difficulty),
                "planned_question_count": sum(row["count"] for row in rows),
                "planned_score": float(sum(Decimal(str(r["score"])) * r["count"] for r in rows)),
            },
            "picked": {
                "question_count": len(flat),
                "total_score": float(total_score),
                "score_by_type": quota_table,
            },
            "difficulty": {
                "target": float(target_difficulty),
                "plan_mix": mix,
                "actual_mix": actual_mix,
                "weighted_average": PaperComposeService._weighted_difficulty(flat),
            },
            "knowledge": {
                "covered": len({k for item in flat for k in item["knowledge_point_ids"]}),
                "pool_size": sum(len(v) for v in pool.values()),
            },
            "shortage": shortage,
            "adjusted": adjusted,
            "feasible": not shortage,
        }
        report["difficulty"]["deviation"] = round(
            report["difficulty"]["weighted_average"] - float(target_difficulty), 2
        )

        # 扁平字段：让「试算」和「已生成」两条路径返回的 report 长得一模一样。
        # compose() 落库后会用库里的真实值再覆盖一次，这里先按内存结果算一份。
        # 否则前端调同名的 ai-compose 接口，dry_run 与否要写两套取数逻辑。
        report["question_count"] = len(flat)
        report["total_score"] = float(total_score)
        report["actual_difficulty"] = report["difficulty"]["weighted_average"]
        report["difficulty_deviation"] = report["difficulty"]["deviation"]
        report["elapsed_ms"] = int((time.time() - started) * 1000)
        report["seed"] = seed

        return {
            "selected": flat,
            "picked": picked,
            "quota_table": quota_table,
            "total_score": total_score.quantize(Decimal("0.01")),
            "report": report,
        }

    # ------------------------------------------------------------------
    # 候选池
    # ------------------------------------------------------------------
    @staticmethod
    def build_pool(*, subject: str = "", knowledge_point_ids=None, exclude_ids=None) -> dict:
        """按题型建候选池。

        默认只取启用中的题目；``exclude_ids`` 用来排除「刚考过」的题（模块八
        的防重复考卷会用到）。知识点过滤是**包含子知识点**的——教师选了
        「树」，自然希望「二叉树」「哈夫曼树」的题也进来。
        """
        queryset = Question.objects.filter(is_active=True)

        if subject:
            queryset = queryset.filter(subject=subject)

        point_ids = [int(i) for i in (knowledge_point_ids or []) if i not in (None, "")]
        if point_ids:
            from apps.questions.models import KnowledgePoint

            expanded = set(point_ids)
            for point in KnowledgePoint.objects.filter(id__in=point_ids):
                expanded.update(point.descendant_ids())
            queryset = queryset.filter(knowledge_points__id__in=expanded)

        if exclude_ids:
            queryset = queryset.exclude(id__in=[int(i) for i in exclude_ids])

        queryset = queryset.prefetch_related("knowledge_points").distinct()

        pool = {}
        for question in queryset:
            pool.setdefault(question.type, []).append(question)

        logger.debug("组卷候选池：%s", {k: len(v) for k, v in pool.items()})
        return pool

    # ------------------------------------------------------------------
    # 抽题
    # ------------------------------------------------------------------
    @staticmethod
    def _take(*, candidates, level, need, kp_usage, rng, locked_ids, target=None) -> list:
        """从候选里抽 ``need`` 道题。

        ``level=None`` 表示不限难度（回退补齐时用），此时按「离目标难度近」做次级排序，
        免得补进来的题把整卷难度拽跑偏。

        每道题的「代价」= 它所挂知识点中**本卷用得最少**的那个的使用次数。
        优先取代价最小的题，等价于让题目自动流向还没怎么考到的知识点。
        """
        available = [
            q for q in candidates
            if q.id not in locked_ids and (level is None or int(q.difficulty) == int(level))
        ]
        if not available:
            return []

        rng.shuffle(available)  # 先打散，保证同代价的题目是随机挑的
        if level is None and target is not None:
            # 回退阶段以难度为先：走到这里的题本来就是「配比没排上」的，
            # 若还按知识点优先挑，会把整卷难度进一步带偏。
            available.sort(key=lambda q: (
                abs(int(q.difficulty) - float(target)),
                PaperComposeService._kp_cost(q, kp_usage),
            ))
        else:
            available.sort(key=lambda q: PaperComposeService._kp_cost(q, kp_usage))

        chosen = []
        for question in available[:need]:
            locked_ids.add(question.id)
            kp_ids = [kp.id for kp in question.knowledge_points.all()]
            for kp_id in kp_ids:
                kp_usage[kp_id] = kp_usage.get(kp_id, 0) + 1
            chosen.append({
                "question": question,
                "difficulty": int(question.difficulty),
                "score": question.score,
                "knowledge_point_ids": kp_ids,
            })
        return chosen

    @staticmethod
    def _kp_cost(question, kp_usage) -> int:
        kp_ids = [kp.id for kp in question.knowledge_points.all()]
        if not kp_ids:
            return 0
        return min(kp_usage.get(kp_id, 0) for kp_id in kp_ids)

    # ------------------------------------------------------------------
    # 难度配比
    # ------------------------------------------------------------------
    @staticmethod
    def plan_difficulty_mix(target_difficulty) -> dict:
        """按目标难度插值出四档配比。

        用「三角核」而不是硬编码几套配比表——目标难度是连续的（教师可能填 1.5），
        硬编码表只能覆盖整数档。权重 w(d) = max(0, 1 - |d - target| / 1.5)，
        归一化后即配比。

        实测效果：目标 1.0 → 简单 75%/中等 25%；2.0 → 20/60/20；3.0 → 20/60/20（偏难侧）。
        """
        target = float(target_difficulty or 2.0)
        raw = {level: max(0.0, 1 - abs(level - target) / 1.5) for level in DIFFICULTY_LEVELS}
        total = sum(raw.values())
        if total <= 0:
            return {level: (1.0 if level == round(target) else 0.0) for level in DIFFICULTY_LEVELS}
        return {level: round(weight / total, 4) for level, weight in raw.items()}

    @staticmethod
    def _split_by_mix(count: int, mix: dict) -> dict:
        """把题量按配比切成整数（最大余数法，保证各档之和恰好等于 count）。"""
        exact = {level: count * float(mix.get(level, 0)) for level in DIFFICULTY_LEVELS}
        quota = {level: int(value) for level, value in exact.items()}

        remainder = count - sum(quota.values())
        if remainder > 0:
            order = sorted(
                DIFFICULTY_LEVELS,
                key=lambda level: (exact[level] - quota[level], mix.get(level, 0)),
                reverse=True,
            )
            for level in order[:remainder]:
                quota[level] += 1
        return quota

    @staticmethod
    def _rebalance_quota(quota: dict, candidates: list, target) -> dict:
        """按题库真实库存校正配额。

        理想配比和真实题库必然打架：如果判断题一道困难题都没有，硬要 6 道困难，
        结果只能是全部走回退、把整卷难度拽跑偏。所以先把「超出库存」的配额削掉，
        再按「离目标难度由近及远」的顺序，把缺口补到还有余量的档位上。

        补不掉的缺口留给调用方的回退逻辑处理——那才是真的抽不满。
        """
        stock = {level: 0 for level in DIFFICULTY_LEVELS}
        for question in candidates:
            level = int(question.difficulty)
            if level in stock:
                stock[level] += 1

        fixed = dict(quota)
        overflow = 0
        for level in DIFFICULTY_LEVELS:
            if fixed[level] > stock[level]:
                overflow += fixed[level] - stock[level]
                fixed[level] = stock[level]

        for level in sorted(DIFFICULTY_LEVELS, key=lambda x: abs(x - float(target))):
            if overflow <= 0:
                break
            room = stock[level] - fixed[level]
            if room <= 0:
                continue
            taken = min(room, overflow)
            fixed[level] += taken
            overflow -= taken

        return fixed

    # ------------------------------------------------------------------
    # 统计与文案
    # ------------------------------------------------------------------
    @staticmethod
    def _weighted_difficulty(items) -> float:
        """按分值加权的平均难度（与 ``Paper.actual_difficulty`` 口径一致）。"""
        total = sum(float(item["score"] or 0) for item in items)
        if total <= 0:
            return 0.0
        return round(sum(item["difficulty"] * float(item["score"] or 0) for item in items) / total, 2)

    @staticmethod
    def _mix_of(items) -> dict:
        bucket = {level: 0 for level in DIFFICULTY_LEVELS}
        for item in items:
            bucket[item["difficulty"]] = bucket.get(item["difficulty"], 0) + 1
        total = len(items) or 1
        return {level: round(value / total, 4) for level, value in bucket.items()}

    @staticmethod
    def auto_blueprint(*, subject: str = "", knowledge_point_ids=None,
                       total_count: int = 20) -> list:
        """按题库库存自动生成一份蓝图（随机组卷用）。

        与 DEFAULT_BLUEPRINT 的「固定题型配比」不同，这里完全跟着题库走：
        哪个题型库存多就多抽、库存少就少抽。教师勾几个知识点随手出一张练习卷，
        不需要自己配蓝图，也不会因为配了题库里没有的题型而组不出来。

        结果只由库存决定（确定性），不引入随机——随机发生在 compose() 抽题阶段，
        由 seed 控制，保证「相同种子 + 相同库存 → 相同卷子」依然成立。
        """
        total_count = int(total_count or 0)
        if total_count <= 0:
            return []

        pool = PaperComposeService.build_pool(
            subject=subject, knowledge_point_ids=knowledge_point_ids,
        )
        stocks = {code: len(items) for code, items in pool.items() if items}
        if not stocks:
            return []

        # 只给有库存的题型分配题量；请求超过库存时封顶到库存，「有多少抽多少」
        types = [code for code in SECTION_TYPE_ORDER if code in stocks]
        total_stock = sum(stocks[t] for t in types)
        cap = min(total_count, total_stock)

        # 按库存占比分配，最大余数法保证总和精确等于 cap
        quota = {}
        assigned = 0
        for t in types:
            q = int(cap * stocks[t] / total_stock)
            quota[t] = q
            assigned += q
        remainder = cap - assigned
        order = sorted(types, key=lambda t: (cap * stocks[t] / total_stock) % 1, reverse=True)
        for t in order[:remainder]:
            quota[t] = quota.get(t, 0) + 1

        blueprint = []
        for t in SECTION_TYPE_ORDER:
            count = quota.get(t, 0)
            if count > 0:
                blueprint.append({
                    "type": t,
                    "count": count,
                    "score": RANDOM_SCORE_BY_TYPE.get(t, 2),
                })
        return blueprint

    @staticmethod
    def _normalize_blueprint(blueprint) -> list:
        """校验并规整蓝图，顺带算出「第几大题」的展示标签。"""
        # 区分「没传」（用默认蓝图）和「传了个空的」（配置错误，必须报出来）。
        # 写成 `blueprint or DEFAULT_BLUEPRINT` 会把空列表也当成没传，
        # 调用方明明配错了却拿到一份「看起来正常」的卷子，这种静默兜底比报错危险。
        rows = DEFAULT_BLUEPRINT if blueprint is None else blueprint
        normalized = []
        for raw in rows:
            code = str(raw.get("type") or "").strip()
            if code not in dict(QuestionType.choices):
                raise BusinessError(f"蓝图里出现了未知题型：{code}", code=13011)
            count = int(raw.get("count") or 0)
            if count <= 0:
                continue
            score = Decimal(str(raw.get("score") or 2))
            if score <= 0:
                raise BusinessError("每题分值必须大于 0", code=13011)
            normalized.append({
                "type": code,
                "label": SECTION_TITLE_BY_TYPE.get(code, code),
                "count": count,
                "score": score,
            })

        if not normalized:
            raise BusinessError("组卷蓝图是空的，至少要有一种题型", code=13011)

        # 按固定题型顺序排，保证卷面大题顺序稳定
        normalized.sort(key=lambda row: SECTION_TYPE_ORDER.index(row["type"]))
        return normalized

    @staticmethod
    def _summary(rows, flat, total_score, target_difficulty) -> str:
        type_text = "、".join(f"{row['label']} {row['count']} 题" for row in rows if row["count"])
        return (
            f"本卷由系统按组卷蓝图自动生成：{type_text}，共 {len(flat)} 题、"
            f"{total_score} 分，目标难度系数 {target_difficulty}。"
            f"选题时已按知识点轮转，尽量让各知识点的考查次数保持均衡。"
        )

    @staticmethod
    def _default_title(subject: str, plan, label: str = "智能组卷") -> str:
        from django.utils import timezone

        stamp = timezone.localtime().strftime("%m%d %H:%M")
        prefix = f"{subject} " if subject else ""
        return f"{prefix}{label} · {plan['report']['picked']['question_count']}题 · {stamp}"

    # ------------------------------------------------------------------
    @staticmethod
    def describe_pool(*, subject: str = "", knowledge_point_ids=None) -> dict:
        """题库家底：各题型各难度的库存矩阵。

        组卷工作台左侧拿它渲染「可用题量」，也让教师一眼看出哪个格子是空的
        （比如「困难档的填空题一道都没有」）。
        """
        pool = PaperComposeService.build_pool(
            subject=subject, knowledge_point_ids=knowledge_point_ids,
        )
        matrix, totals = {}, {}
        for code, questions in pool.items():
            row = {level: 0 for level in DIFFICULTY_LEVELS}
            for question in questions:
                row[int(question.difficulty)] = row.get(int(question.difficulty), 0) + 1
            matrix[code] = row
            totals[code] = len(questions)

        return {
            "subject": subject,
            "total": sum(totals.values()),
            "by_type": [
                {
                    "type": code,
                    "label": SECTION_TITLE_BY_TYPE.get(code, code),
                    "total": totals[code],
                    "by_difficulty": matrix[code],
                }
                for code in SECTION_TYPE_ORDER if code in matrix
            ],
        }


__all__ = ["DEFAULT_BLUEPRINT", "DIFFICULTY_LEVELS", "PaperComposeService"]
