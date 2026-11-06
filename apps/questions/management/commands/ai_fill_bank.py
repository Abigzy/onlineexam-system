"""用 AI 批量扩充题库。

为什么需要它：
    种子数据只有 19 道题，够跑通流程，但撑不起智能组卷——组卷要按「知识点覆盖」
    +「难度配比」抽题，单选题只有 4 道的时候，任何算法都变不出第 5 道。

用法：
    python manage.py ai_fill_bank --limit 1        # 先拿 1 个知识点试水，看看质量
    python manage.py ai_fill_bank                  # 全量（约 20 知识点 × 2 轮 × 5 题）
    python manage.py ai_fill_bank --rounds 3       # 再加一轮高难题
    python manage.py ai_fill_bank --dry-run        # 只生成不入库、不花数据库
    python manage.py ai_fill_bank --knowledge-point 二叉树 --knowledge-point 图
    python manage.py ai_fill_bank --skip-existing 8  # 已有 8 道以上的知识点直接跳过
    python manage.py ai_fill_bank --subject 计算机组成原理 --target 92  # 精确新增 92 道

两种模式：
  * **轮次模式**（默认）——按「知识点 × 轮次 × 每轮题数」推进，跑完所有组合就停。
    总量是**估算**的：AI 每次返回几道就入库几道，实际条数不可控。
  * **目标模式**（``--target N``）——以「本次新增 N 道」为终止条件，够数即停；
    配额按知识点均摊，题型与难度由调度器动态配平。要「补齐到 100 道」这种
    带明确数字的交付，一律用这个模式。

过程是**幂等**的：入库前按题干查重，重复的题自动丢弃，所以中断后重跑不会灌重复题。
"""

import math
import time

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.ai.exceptions import AIServiceError
from apps.common.constants import Difficulty, QuestionType, Role
from apps.common.exceptions import BusinessError
from apps.questions.constants import QuestionSource
from apps.questions.models import KnowledgePoint, Question
from apps.questions.services import QuestionAIService, QuestionService
from apps.users.models import User

# 每一轮的「难度 + 题型组合」。分轮的理由：一轮里让模型同时兼顾六种题型，
# 每种都写不好；拆成几轮，每轮聚焦 3-4 种题型，质量明显更稳。
ROUND_PLANS = [
    {
        "name": "基础轮",
        "difficulty": Difficulty.EASY,
        "types": [QuestionType.SINGLE, QuestionType.MULTIPLE, QuestionType.JUDGE, QuestionType.FILL],
    },
    {
        "name": "进阶轮",
        "difficulty": Difficulty.MEDIUM,
        "types": [QuestionType.SINGLE, QuestionType.MULTIPLE, QuestionType.JUDGE, QuestionType.SHORT],
    },
    {
        "name": "挑战轮",
        "difficulty": Difficulty.HARD,
        "types": [QuestionType.SINGLE, QuestionType.MULTIPLE, QuestionType.ESSAY],
    },
    {
        "name": "拔高轮",
        "difficulty": Difficulty.EXPERT,
        "types": [QuestionType.SINGLE, QuestionType.MULTIPLE, QuestionType.ESSAY],
    },
]

DEFAULT_ROUNDS = 2

# ----------------------------------------------------------------------
# 目标模式的题型调度表
# ----------------------------------------------------------------------
# 每次请求带 2 种题型，比「一次塞 6 种」稳定得多：模型的注意力不会被摊薄，
# 判断题不会写成单选题的样子。这 6 组里 6 种题型**每种恰好出现 2 次**，
# 所以只要每档难度都被用到一次，题型分布天然是均衡的。
TARGET_TYPE_PAIRS = [
    (QuestionType.SINGLE, QuestionType.MULTIPLE),
    (QuestionType.JUDGE, QuestionType.FILL),
    (QuestionType.SHORT, QuestionType.ESSAY),
    (QuestionType.SINGLE, QuestionType.JUDGE),
    (QuestionType.MULTIPLE, QuestionType.FILL),
    (QuestionType.SHORT, QuestionType.ESSAY),
]

# 目标模式的难度档位（每次请求只锁定一个难度，四档轮转）
TARGET_DIFFICULTIES = (Difficulty.EASY, Difficulty.MEDIUM, Difficulty.HARD, Difficulty.EXPERT)

# 目标模式下喂给模型的「已命制题干」条数：给多了会挤掉命题素材的篇幅，
# 给少了起不到避重的作用，8 条是实测比较稳的量。
RECENT_STEM_LIMIT = 8


def _parse_list(value) -> list:
    """把 ``judge,fill`` 或中文逗号分隔的字符串拆成列表。"""
    if not value:
        return []
    text = str(value).replace("，", ",")
    return [token.strip() for token in text.split(",") if token.strip()]


def _parse_int_list(value) -> list:
    """同上，但只保留能转成整数的项。"""
    return [int(token) for token in _parse_list(value) if token.isdigit()]


class Command(BaseCommand):
    help = "调用大模型批量生成题目并入库，用于把题库扩充到可支撑智能组卷的规模"

    def add_arguments(self, parser):
        parser.add_argument("--subject", default="", help="限定学科（默认：所有知识点）")
        parser.add_argument("--knowledge-point", dest="knowledge_points", action="append", default=[],
                            help="限定知识点（名称或 id），可重复传入")
        parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS,
                            help=f"跑前几轮（默认 {DEFAULT_ROUNDS}，最多 {len(ROUND_PLANS)}）")
        parser.add_argument("--per-round", type=int, default=5, help="每轮每个知识点生成几道题（默认 5）")
        parser.add_argument("--difficulty", default="",
                            help="只跑指定难度，可逗号分隔（如 3 或 2,3）")
        parser.add_argument("--types", default="",
                            help="只补指定题型，逗号分隔（如 judge,fill）。与 --difficulty 搭配"
                                 "可定向补题库的难度短板——比如「困难判断题一道都没有」")
        parser.add_argument("--skip-existing", type=int, default=0,
                            help="该知识点已有 N 道以上题目时跳过（默认 0，即不跳过）")
        parser.add_argument("--limit", type=int, default=0, help="最多处理 N 个知识点（调试用）")
        parser.add_argument("--target", type=int, default=0,
                            help="目标模式：本次要**新增**的题目数量（达到即停，精确控制）。"
                                 "例：--subject 计算机组成原理 --target 92")
        parser.add_argument("--max-calls", type=int, default=0,
                            help="目标模式下最多调用 AI 的次数（默认按目标量自动推算），防止 AI 挂掉后死循环")
        parser.add_argument("--delay", type=float, default=0.3, help="每次调用后的间隔秒数（默认 0.3）")
        parser.add_argument("--provider", default=None, help="临时指定 AI 平台（默认读 .env）")
        parser.add_argument("--dry-run", action="store_true", help="只生成不入库")

    # ------------------------------------------------------------------
    def handle(self, *args, **options):
        points = self._resolve_points(options)
        if not points:
            self.stderr.write(self.style.ERROR("没有找到任何知识点，请先执行 seed_questions 或手动创建。"))
            return

        plans = self._resolve_plans(options)
        if not plans:
            self.stderr.write(self.style.ERROR(
                "没有匹配到任何生成计划，请检查 --rounds / --difficulty / --types 参数。"
            ))
            return
        teacher = self._teacher()

        if options["limit"]:
            points = points[: options["limit"]]

        # 目标模式与轮次模式的分岔口：给了 --target 就只走目标模式，
        # 完全避开 ROUND_PLANS 那套「按轮次估算」的逻辑，互不干扰。
        if int(options["target"] or 0) > 0:
            self._run_target_mode(points=points, teacher=teacher, options=options)
            return

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n知识点 {len(points)} 个 × {len(plans)} 轮 × {options['per_round']} 题"
            f"  →  计划生成约 {len(points) * len(plans) * options['per_round']} 道"
        ))
        self.stdout.write(f"入库身份：{teacher}　|　模式：{'DRY-RUN（不入库）' if options['dry_run'] else '写入数据库'}\n")

        stat = {"created": 0, "duplicate": 0, "failed": 0, "ai_calls": 0, "tokens": 0}
        started = time.time()

        for position, point in enumerate(points, 1):
            chain = point.full_path if hasattr(point, "full_path") else point.name
            self.stdout.write(self.style.HTTP_INFO(
                f"[{position}/{len(points)}] {chain}（现有 {point.questions.count()} 题）"
            ))

            if options["skip_existing"] and point.questions.count() >= options["skip_existing"]:
                self.stdout.write(self.style.WARNING(
                    f"    已有 {point.questions.count()} 题，达到阈值，跳过"
                ))
                continue

            for plan in plans:
                self._run_round(point=point, plan=plan, teacher=teacher, stat=stat, options=options)
                if options["delay"]:
                    time.sleep(options["delay"])

        elapsed = time.time() - started
        self._report(stat, elapsed, options)

    # ------------------------------------------------------------------
    # 单轮
    # ------------------------------------------------------------------
    def _run_round(self, *, point, plan, teacher, stat, options):
        """跑一个「知识点 × 难度/题型组合」，成功生成的题直接入库。"""
        count = options["per_round"]
        try:
            result = QuestionAIService.generate_drafts(
                user=teacher,
                subject=point.subject,
                knowledge_points=[point.name],
                question_types=plan["types"],
                difficulty=plan["difficulty"],
                count=count,
                source_text=self._source_text(point),
                provider=options["provider"],
            )
        except (BusinessError, AIServiceError) as exc:
            stat["failed"] += 1
            self.stdout.write(self.style.ERROR(f"    {plan['name']}：AI 调用失败 → {exc}"))
            return

        stat["ai_calls"] += 1
        stat["tokens"] += int((result.get("meta") or {}).get("total_tokens") or 0)

        drafts = result.get("drafts") or []
        if not drafts:
            self.stdout.write(self.style.WARNING(f"    {plan['name']}：未生成有效题目"))
            return

        items, fresh = self._to_items(drafts, point, stat)
        if not fresh:
            self.stdout.write(f"    {plan['name']}：生成 {len(drafts)} 道，全部与题库重复，丢弃")
            return

        if options["dry_run"]:
            stat["created"] += len(fresh)
            self.stdout.write(self.style.SUCCESS(
                f"    {plan['name']}：生成 {len(fresh)} 道（dry-run 未入库）"
                f"　例：{fresh[0]['stem'][:36]}"
            ))
            return

        try:
            created, errors = QuestionService.bulk_create(
                items=fresh, user=teacher, default_subject=point.subject,
            )
        except BusinessError as exc:
            stat["failed"] += 1
            self.stdout.write(self.style.ERROR(f"    {plan['name']}：入库失败 → {exc}"))
            return

        stat["created"] += len(created)
        stat["failed"] += len(errors)
        line = f"    {plan['name']}：入库 {len(created)} 道"
        if errors:
            line += f"，失败 {len(errors)} 道（{errors[0]['error'][:40]}）"
        self.stdout.write(self.style.SUCCESS(line) if not errors else self.style.WARNING(line))

    # ------------------------------------------------------------------
    def _to_items(self, drafts, point, stat):
        """把 AI 草稿转成入库 payload，顺带按题干去重。

        去重是这道命令敢让人反复重跑的关键：AI 每次生成的题面用词会有差异，
        但「完全同一个题干」基本只出现在重复跑同一轮的时候。
        """
        stems = [str(d.get("stem") or "").strip() for d in drafts]
        existing = set(
            Question.objects.filter(stem__in=[s for s in stems if s]).values_list("stem", flat=True)
        )

        items, fresh = [], []
        for draft, stem in zip(drafts, stems):
            if not stem:
                stat["failed"] += 1
                continue
            if stem in existing:
                stat["duplicate"] += 1
                continue
            item = {
                "type": draft.get("type") or QuestionType.SINGLE,
                "stem": stem,
                "difficulty": int(draft.get("difficulty") or Difficulty.MEDIUM),
                "score": draft.get("score") or 2,
                "options": draft.get("options") or [],
                "reference_answer": draft.get("reference_answer") or "",
                "analysis": draft.get("analysis") or "",
                "subject": point.subject,
                "knowledge_point_ids": [point.id],
                "source": QuestionSource.AI,
            }
            items.append(item)
            fresh.append(item)
            existing.add(stem)  # 同一次调用里也不允许重复
        return items, fresh

    # ------------------------------------------------------------------
    def _source_text(self, point) -> str:
        """给模型的命题素材。

        不喂教材原文，而是给出「课程 → 知识路径 → 本题范围」的定位信息。
        实测比空着不给素材好得多：模型知道该按哪门课的哪一章来命题，
        也不会把「栈」误当成别的学科的概念。
        """
        chain = point.full_path if hasattr(point, "full_path") else point.name
        return (
            f"课程：{point.subject or '（未指定）'}\n"
            f"知识路径：{chain}\n"
            f"本题范围：请严格围绕「{point.name}」这一知识点命题，"
            f"可结合其前置知识，但考点必须落在「{point.name}」上。"
        )

    # ------------------------------------------------------------------
    # 目标模式：把题库精确补到「新增 N 道」
    # ------------------------------------------------------------------
    def _run_target_mode(self, *, points, teacher, options):
        """按「本次新增 N 道」精确扩充题库。

        为什么需要单独一条路径：
            轮次模式是按「知识点 × 轮次 × 每轮题数」**估算**总量的，AI 少返回两道、
            或者有几道题被查重丢掉，最后就差那么两三道，永远补不齐。而「题库补齐到
            100 道」这种交付是带明确数字的，必须由命令自己数着来。

        三个控制点：
          * **终止条件** —— ``stat["created"] >= target`` 立即停，不多跑一次调用；
          * **知识点均摊** —— 每个点的配额是 ``ceil(target / 知识点数)``，避免 AI
            在「补码」上连出 40 道、而「CPU」一道没有；
          * **题型/难度配平** —— 每批只锁 1 档难度 + 2 种题型，下一批选「当前产出
            最少」的那个组合，形成负反馈式均衡；偏斜会自动被后续批次补回来。
        """
        target = int(options["target"])
        batch = max(1, int(options["per_round"]))
        point_count = max(1, len(points))
        quota = math.ceil(target / point_count)
        # 调用次数上限：正常 1 次能出 batch 道，留 3 倍余量给「重复丢弃 / 校验失败」，
        # 再兜 10 次；AI 彻底不可用时靠它跳出循环，而不是死等。
        max_calls = max(1, int(options["max_calls"] or (math.ceil(target / batch) * 3 + 10)))

        start_total, start_active = self._subject_counts(options["subject"])
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n目标模式：{options['subject'] or '全部知识点'} 新增 {target} 道"
        ))
        self.stdout.write(
            f"知识点 {point_count} 个　单点配额 {quota} 道　单次请求 {batch} 道　"
            f"调用上限 {max_calls} 次"
        )
        self.stdout.write(
            f"入库身份：{teacher}　|　模式：{'DRY-RUN（不入库）' if options['dry_run'] else '写入数据库'}"
            f"　|　起始题量：{start_total} 道（启用 {start_active}）\n"
        )

        stat = {"created": 0, "duplicate": 0, "failed": 0, "rejected": 0, "ai_calls": 0, "tokens": 0}
        produced_types, produced_difficulties = {}, {}
        per_point = {point.id: 0 for point in points}
        calls, overflow, elapsed = 0, False, 0.0
        started = time.time()

        while stat["created"] < target and calls < max_calls:
            progressed = False

            for position, point in enumerate(points, 1):
                if stat["created"] >= target or calls >= max_calls:
                    break

                # 正常阶段严守单点配额；一整轮下来零产出（AI 抖动 / 全被查重）才
                # 放开配额，让还有余力的知识点把缺口补上。
                allowance = quota - per_point[point.id]
                if allowance <= 0 and not overflow:
                    continue
                take = min(batch, target - stat["created"], allowance if allowance > 0 else batch)
                if take <= 0:
                    continue

                difficulty, types = self._pick_slot(produced_difficulties, produced_types)
                created_now, snapshot = self._generate_batch(
                    point=point, types=types, difficulty=difficulty, count=take,
                    teacher=teacher, options=options, stat=stat,
                )
                calls += 1
                per_point[point.id] += created_now

                for item in snapshot:
                    produced_types[item["type"]] = produced_types.get(item["type"], 0) + 1
                    level = int(item["difficulty"])
                    produced_difficulties[level] = produced_difficulties.get(level, 0) + 1

                if created_now:
                    progressed = True

                self.stdout.write(
                    f"  [{position}/{point_count}] {point.name}　"
                    f"{Difficulty(difficulty).label}·{'/'.join(QuestionType(t).label for t in types)}　"
                    f"→ 新增 {created_now} 道　"
                    f"累计 {stat['created']}/{target}"
                )
                if options["delay"] and stat["created"] < target:
                    time.sleep(options["delay"])

            if stat["created"] >= target:
                break
            if not progressed:
                if overflow:
                    break  # 放宽配额后仍然零产出 → AI 不可用，如实报缺口
                overflow = True
                self.stdout.write(self.style.WARNING(
                    "  一轮下来零新增，放宽知识点配额重试…"
                ))

        elapsed = time.time() - started
        self._report_target(
            stat=stat, elapsed=elapsed, options=options, target=target,
            produced_types=produced_types, produced_difficulties=produced_difficulties,
            start_total=start_total, start_active=start_active, calls=calls,
        )

    # ------------------------------------------------------------------
    def _pick_slot(self, produced_difficulties: dict, produced_types: dict):
        """挑下一个「难度 + 题型组合」——总是选当前产出最少的那个。

        打分 = 该难度的已产出量 + 该组题型已产出量的一半。
        权重这样取的原因：一次请求只会落进 1 档难度，但会在 2 种题型间分摊，
        所以难度维度天然比题型维度「重」一倍，各除以「覆盖面」后才可比。
        好处是自纠正——某一档因为模型不听话多产了几道，下一轮自然就跳过它。
        """
        best = None
        best_score = None
        for pair in TARGET_TYPE_PAIRS:
            type_pressure = sum(produced_types.get(t, 0) for t in pair) / 2.0
            for level in TARGET_DIFFICULTIES:
                score = produced_difficulties.get(int(level), 0) + type_pressure
                if best_score is None or score < best_score - 1e-9:
                    best_score = score
                    best = (int(level), list(pair))
        return best if best else (int(Difficulty.MEDIUM), [QuestionType.SINGLE, QuestionType.MULTIPLE])

    # ------------------------------------------------------------------
    def _generate_batch(self, *, point, types, difficulty, count, teacher, options, stat):
        """生成一批题并入库，返回 ``(新增条数, 新增题目快照)``。

        与 ``_run_round`` 的区别只有两点，其余校验链路完全一致（都走
        ``QuestionService.bulk_create``，空题干 / 无正确答案 / 单选题多选都会被拦）：
          * 返回值要带回「实际新增了什么」，供调度器统计题型与难度分布；
          * 多一道**选项内容查重**：AI 偶尔把同一个选项文案挂到 A、B 两个标号上，
            这类题技术上「能入库」，但对学生毫无意义，直接拦掉。
        """
        try:
            result = QuestionAIService.generate_drafts(
                user=teacher,
                subject=point.subject,
                knowledge_points=[point.name],
                question_types=types,
                difficulty=difficulty,
                count=count,
                source_text=self._target_source_text(point),
                provider=options["provider"],
            )
        except (BusinessError, AIServiceError) as exc:
            stat["failed"] += 1
            self.stdout.write(self.style.ERROR(f"    AI 调用失败 → {exc}"))
            return 0, []

        stat["ai_calls"] += 1
        stat["tokens"] += int((result.get("meta") or {}).get("total_tokens") or 0)

        drafts = result.get("drafts") or []
        if not drafts:
            self.stdout.write(self.style.WARNING("    AI 未返回有效题目"))
            return 0, []

        items, fresh = self._to_items(drafts, point, stat)
        clean = []
        for item in fresh:
            if self._has_duplicate_options(item):
                stat["rejected"] += 1
                self.stdout.write(self.style.WARNING("    选项内容重复，已丢弃 1 道"))
                continue
            clean.append(item)
        if not clean:
            return 0, []

        if options["dry_run"]:
            stat["created"] += len(clean)
            return len(clean), [self._snapshot_of(item) for item in clean]

        try:
            created, errors = QuestionService.bulk_create(
                items=clean, user=teacher, default_subject=point.subject,
            )
        except BusinessError as exc:
            stat["failed"] += 1
            self.stdout.write(self.style.ERROR(f"    入库失败 → {exc}"))
            return 0, []

        stat["created"] += len(created)
        stat["failed"] += len(errors)
        if errors:
            self.stdout.write(self.style.WARNING(
                f"    入库失败 {len(errors)} 道（{errors[0]['error'][:40]}）"
            ))
        return len(created), [self._snapshot_of(question) for question in created]

    # ------------------------------------------------------------------
    @staticmethod
    def _snapshot_of(source) -> dict:
        """把「入库后的 Question」或「待入库的 payload」统一成统计用的三元组。"""
        if isinstance(source, dict):
            return {
                "type": source.get("type") or QuestionType.SINGLE,
                "difficulty": int(source.get("difficulty") or Difficulty.MEDIUM),
                "stem": source.get("stem") or "",
            }
        return {
            "type": source.type,
            "difficulty": int(source.difficulty),
            "stem": source.stem,
        }

    @staticmethod
    def _has_duplicate_options(item: dict) -> bool:
        """选项内容是否重复（忽略大小写与首尾空白）。

        判断题固定 A=正确 / B=错误，天然不重复，不受影响。
        """
        contents = [
            str(option.get("content") or "").strip().lower()
            for option in (item.get("options") or [])
        ]
        return len(contents) != len(set(contents))

    def _target_source_text(self, point) -> str:
        """目标模式喂给模型的素材 = 定位信息 + 该知识点最近的题干（用来避重）。

        「最近题干」这部分在批量补题时很关键：同一个知识点要连出十几道，
        不给模型看看已经出过什么，它会连着三批都写「下列关于 X 的说法正确的是」。
        """
        base = self._source_text(point)
        recent = list(
            Question.objects.filter(knowledge_points__id=point.id)
            .order_by("-id")
            .values_list("stem", flat=True)[:RECENT_STEM_LIMIT]
        )
        if not recent:
            return base
        lines = "\n".join(f"- {stem[:60]}" for stem in recent)
        return (
            f"{base}\n"
            f"已命制过的题干（**不得重复命题**，请换考点角度、换情境、换设问方式）：\n{lines}"
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _subject_counts(subject: str) -> tuple:
        """返回 ``(总数, 启用数)``；给了学科就只看该学科。"""
        queryset = Question.objects.filter(subject=subject) if subject else Question.objects.all()
        return queryset.count(), queryset.filter(is_active=True).count()

    def _report_target(self, *, stat, elapsed, options, target, produced_types,
                       produced_difficulties, start_total, start_active, calls):
        """目标模式的结果报告：达成率 + 本次新增的题型/难度分布 + 学科题库现状。"""
        created = stat["created"]
        subject = options["subject"]

        self.stdout.write("\n" + "=" * 56)
        self.stdout.write(self.style.MIGRATE_HEADING(f"目标模式结果（目标新增 {target} 道）"))
        self.stdout.write(f"  实际新增　：{created} 道")
        self.stdout.write(f"  重复丢弃　：{stat['duplicate']} 道")
        self.stdout.write(f"  质量拦截　：{stat['rejected']} 道（选项内容重复）")
        self.stdout.write(f"  失败条目　：{stat['failed']} 条")
        self.stdout.write(f"  AI 调用　 ：{calls} 次，约 {stat['tokens']} tokens")
        self.stdout.write(f"  耗时　　　：{elapsed:.1f} 秒")

        if created >= target:
            self.stdout.write(self.style.SUCCESS(f"  达成情况　：达标 {created}/{target}"))
        else:
            self.stdout.write(self.style.ERROR(
                f"  达成情况　：未达标，还差 {target - created} 道（{created}/{target}）"
            ))

        if options["dry_run"]:
            self.stdout.write("\n（DRY-RUN：以上为预演数据，未写入数据库）")
            self.stdout.write("=" * 56)
            return

        total, active = self._subject_counts(subject)
        self.stdout.write(
            f"\n  {subject or '全库'}题量：总数 {total} 道（本次 +{total - start_total}），"
            f"启用 {active} 道"
        )

        self.stdout.write("\n  本次新增按题型：")
        for code, label in QuestionType.choices:
            number = produced_types.get(code, 0)
            if not number:
                continue
            self.stdout.write(f"   {label:<8}{number:>5}  {'█' * min(30, number)}")

        self.stdout.write("\n  本次新增按难度：")
        for level, label in Difficulty.choices:
            number = produced_difficulties.get(int(level), 0)
            if not number:
                continue
            self.stdout.write(f"   {label:<8}{number:>5}  {'█' * min(30, number)}")

        self.stdout.write(f"\n  {subject or '全库'}按题型现状（启用）：")
        scope = Question.objects.filter(is_active=True)
        if subject:
            scope = scope.filter(subject=subject)
        for code, label in QuestionType.choices:
            number = scope.filter(type=code).count()
            self.stdout.write(f"   {label:<8}{number:>5}  {'█' * min(30, number)}")
        self.stdout.write("=" * 56)

    # ------------------------------------------------------------------
    # 参数解析
    # ------------------------------------------------------------------
    def _resolve_points(self, options) -> list:
        queryset = KnowledgePoint.objects.all()
        if options["subject"]:
            queryset = queryset.filter(subject=options["subject"])

        wanted = [str(k).strip() for k in options["knowledge_points"] if str(k).strip()]
        if wanted:
            from django.db.models import Q

            condition = Q()
            for token in wanted:
                if token.isdigit():
                    condition |= Q(id=int(token))
                else:
                    condition |= Q(name=token)
            queryset = queryset.filter(condition)

        # 按「学科 → 层级 → id」排序，让输出看起来像在遍历知识树
        return list(queryset.order_by("subject", "parent_id", "id"))

    @staticmethod
    def _resolve_plans(options) -> list:
        """决定跑哪几轮。

        两种模式：
          * 默认 —— 按 ``ROUND_PLANS`` 的预设轮次跑前 N 轮；
          * 定向 —— 给了 ``--types`` 就按「指定题型 × 指定难度」跑，
                    用来精准补题库的短板（某题型某难度一道题都没有的情况）。
        """
        levels = _parse_int_list(options.get("difficulty"))
        custom_types = _parse_list(options.get("types"))

        if custom_types:
            unknown = [t for t in custom_types if t not in dict(QuestionType.choices)]
            if unknown:
                raise BusinessError(f"未知题型：{'、'.join(unknown)}", code=12003)
            return [
                {"name": f"定向补题（难度 {level}）", "difficulty": level, "types": custom_types}
                for level in (levels or [Difficulty.MEDIUM])
            ]

        plans = ROUND_PLANS[: max(1, min(options["rounds"], len(ROUND_PLANS)))]
        if levels:
            plans = [plan for plan in plans if int(plan["difficulty"]) in levels]
        return plans

    @staticmethod
    def _teacher():
        user = User.objects.filter(role=Role.TEACHER).order_by("id").first()
        if user is None:
            user = User.objects.filter(is_superuser=True).order_by("id").first()
        if user is None:
            raise BusinessError("数据库里没有教师账号，请先执行 python manage.py seed_users")
        return user

    # ------------------------------------------------------------------
    def _report(self, stat, elapsed, options):
        self.stdout.write("\n" + "=" * 52)
        self.stdout.write(self.style.MIGRATE_HEADING("本次扩充结果"))
        self.stdout.write(f"  入库题目　：{stat['created']} 道")
        self.stdout.write(f"  重复丢弃　：{stat['duplicate']} 道")
        self.stdout.write(f"  失败条目　：{stat['failed']} 条")
        self.stdout.write(f"  AI 调用　 ：{stat['ai_calls']} 次，约 {stat['tokens']} tokens")
        self.stdout.write(f"  耗时　　　：{elapsed:.1f} 秒")

        if not options["dry_run"]:
            total = Question.objects.count()
            self.stdout.write(f"\n 题库当前共 {total} 道题，按题型：")
            for code, label in QuestionType.choices:
                number = Question.objects.filter(type=code, is_active=True).count()
                bar = "█" * min(40, number) if number else ""
                self.stdout.write(f"   {label:<8}{number:>5}  {bar}")
        self.stdout.write("=" * 52)


__all__ = ["Command"]
