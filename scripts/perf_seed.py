"""模块 11 · 性能测试数据准备脚本。

**只写压测库，不碰演示库。** 默认库名 ``online_exam_perf``，
派生自 ``PERF_DB_NAME`` 环境变量；如果解析出来的库名等于 ``online_exam``
（``.env`` 里的演示库），脚本直接退出并报错，而不是"小心一点"。

用法（Git Bash，在项目根目录执行）::

    # 1) 建库 + 迁移 + 建压测数据（管理员 / 教师 / 30 名学生 / 1 份客观题卷 / 1 场进行中考试）
    ./.venv/Scripts/python.exe scripts/perf_seed.py setup

    # 2) AC-36 用：再灌 1 万条学生（独立的库，跑完可整库丢弃）
    ./.venv/Scripts/python.exe scripts/perf_seed.py users --count 10000

    # 3) 只清数据、保留库结构
    ./.venv/Scripts/python.exe scripts/perf_seed.py cleanup

    # 4) 连库一起删（推荐，压测库本来就是一次性的）
    ./.venv/Scripts/python.exe scripts/perf_seed.py cleanup --drop-db

产出物：

  * ``scripts/.perf_meta.json``  考试 ID / 试卷 ID / 学生名单 / 各角色账号
  * ``scripts/.perf_tokens.json`` 每个压测账号的 access token（压测时直接复用，
    避免 locust 每个虚拟用户都调一次登录接口 —— 登录接口有 ``auth`` 限流 30/min，
    全部来自 127.0.0.1 时会被 429 打断，压测结果就不真实了）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

PERF_DB_DEFAULT = "online_exam_perf"
DEMO_DB = "online_exam"

META_FILE = BASE_DIR / "scripts" / ".perf_meta.json"
TOKEN_FILE = BASE_DIR / "scripts" / ".perf_tokens.json"

# 压测库里的统一口令。压测库是一次性数据，密码不需要保密；
# 但脚本**不允许**用它去连演示库（见 _resolve_db_name）。
PERF_PASSWORD = "Perf@123456"

USER_PREFIX = "perf_"
QUESTION_MARK = "[PERF]"

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "OnlineExam.settings.development")


# ----------------------------------------------------------------------
# 库名解析：这是本脚本最重要的安全护栏
# ----------------------------------------------------------------------
def _resolve_db_name(explicit: str | None = None) -> str:
    """决定这次要连哪个库，并在连演示库时直接终止。"""
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")  # 只补齐缺省值，不覆盖已有环境变量

    name = explicit or os.environ.get("PERF_DB_NAME") or PERF_DB_DEFAULT
    if name == DEMO_DB:
        sys.exit(
            f"拒绝执行：压测库名不能是演示库 {DEMO_DB!r}。\n"
            f"请用 PERF_DB_NAME=online_exam_perf 或在命令里 --db 指定一个独立库名。"
        )
    return name


def _mysql_params() -> dict:
    return {
        "host": os.environ.get("DB_HOST", "127.0.0.1"),
        "port": int(os.environ.get("DB_PORT", "3306")),
        "user": os.environ.get("DB_USER", "root"),
        "password": os.environ.get("DB_PASSWORD", ""),
        "charset": "utf8",
    }


def _bootstrap(db_name: str, *, create: bool) -> None:
    """在 import settings 之前把 DB_NAME 定死，再让 Django 起来。"""
    os.environ["DB_NAME"] = db_name
    if create:
        import pymysql

        params = _mysql_params()
        conn = pymysql.connect(**params)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
                    f"DEFAULT CHARACTER SET utf8 COLLATE utf8_general_ci"
                )
            conn.commit()
        finally:
            conn.close()

    import django

    django.setup()


def _drop_database(db_name: str) -> None:
    import pymysql

    params = _mysql_params()
    conn = pymysql.connect(**params)
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
        conn.commit()
    finally:
        conn.close()


def _migrate() -> None:
    from django.core.management import call_command

    print("  · 迁移数据库结构（migrate，静默）…")
    started = time.time()
    call_command("migrate", verbosity=0, interactive=False)
    print(f"  · 迁移完成，用时 {time.time() - started:.1f}s")


# ----------------------------------------------------------------------
# setup：管理员 / 教师 / 学生 / 客观题卷 / 进行中考试
# ----------------------------------------------------------------------
def _ensure_user(username: str, *, role: str, real_name: str, password: str, **extra):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.filter(username=username).first()
    if user:
        return user, False
    user = User(
        username=username,
        email=f"{username}@perf.local",
        real_name=real_name,
        role=role,
        **extra,
    )
    user.set_password(password)
    user.save()
    return user, True


def _ensure_students(count: int) -> list:
    """批量建学生。密码哈希只算一次再复用 —— 1 万条逐条 set_password 要跑几十分钟。"""
    from django.contrib.auth import get_user_model
    from django.contrib.auth.hashers import make_password

    User = get_user_model()
    existing = {
        name: pk
        for name, pk in User.objects.filter(username__startswith=f"{USER_PREFIX}stu_")
        .values_list("username", "pk")
    }
    todo = []
    for index in range(1, count + 1):
        username = f"{USER_PREFIX}stu_{index:03d}"
        if username in existing:
            continue
        todo.append(
            User(
                username=username,
                email=f"{username}@perf.local",
                real_name=f"压测学生{index:03d}",
                role="student",
                student_no=f"PERF{index:05d}",
                # 学生不填 subject：/manage/users/ 的学科候选是把整张 users 表
                # 里 subject 非空的行拉回来去重，学生留空可以让 AC-36 的测量
                # 只反映"1 万条用户"本身，而不是被 1 万行无用数据拖慢网络传输
                subject="",
                password=make_password(PERF_PASSWORD),
            )
        )
    if todo:
        User.objects.bulk_create(todo, batch_size=500)
    return list(
        User.objects.filter(username__startswith=f"{USER_PREFIX}stu_").order_by("username")
    )


def _ensure_questions(count: int = 20) -> list:
    """造一批纯客观题（单选）。**不含任何主观题**，判分全程不调 AI。"""
    from decimal import Decimal

    from apps.questions.models import Question, QuestionOption

    created = []
    for index in range(1, count + 1):
        stem = f"{QUESTION_MARK} 性能测试题 {index:02d}：以下哪一项是正确答案？"
        question = Question.objects.filter(stem=stem).first()
        if question is None:
            question = Question.objects.create(
                type="single",
                stem=stem,
                difficulty=2,
                score=Decimal("5.00"),
                reference_answer="A",
                analysis="压测用固定题目，无业务含义。",
                subject="性能测试",
                created_by=None,
                is_active=True,
                is_reviewed=True,
            )
            QuestionOption.objects.bulk_create([
                QuestionOption(question=question, label=label, content=f"选项 {label}",
                               is_correct=(label == "A"), order=order)
                for order, label in enumerate("ABCD", start=1)
            ])
        created.append(question)
    return created


def _ensure_paper(questions: list, *, title: str = f"{QUESTION_MARK} 客观题压测卷"):
    from apps.papers.models import Paper
    from apps.papers.services import PaperService

    paper = Paper.objects.filter(title=title).first()
    if paper is not None:
        PaperService.recalc(paper)
        return paper

    paper = PaperService.create(
        data={
            "title": title,
            "subject": "性能测试",
            "description": "模块 11 压测专用：全部为单选题，判分不调用大模型。",
            "duration_minutes": 120,
            "pass_score": 60,
            "question_ids": [item.id for item in questions],
        },
    )
    PaperService.change_status(paper, "published")  # 发布后才能被考试引用
    return paper


def _ensure_exam(paper, teacher, *, title: str = f"{QUESTION_MARK} 60 人并发考试"):
    from datetime import timedelta

    from django.utils import timezone

    from apps.exam.models import Exam
    from apps.exam.services import ExamService

    now = timezone.now()
    exam = Exam.objects.filter(title=title).first()
    if exam is not None:
        exam.paper = paper
        exam.start_time = now - timedelta(minutes=5)
        exam.end_time = now + timedelta(hours=6)
        exam.status = "published"
        exam.save()
        return exam

    exam = ExamService.create(
        data={
            "title": title,
            "description": "模块 11 压测专用考试，勿用于演示。",
            "subject": "性能测试",
            "paper": paper,
            "start_time": now - timedelta(minutes=5),
            "end_time": now + timedelta(hours=6),
            "duration_minutes": 120,
            # 交卷场景里同一个虚拟用户会反复"开考→交卷"，次数上限给足，
            # 否则跑到一半开始返回 104xx「作答次数已用完」，失败率被业务规则污染
            "max_attempts": 200,
            "open_to_all": True,
            "show_score_immediately": False,
            "shuffle_questions": False,
            "shuffle_options": False,
        },
        user=teacher,
    )
    ExamService.change_status(exam, "published")
    return exam


def _issue_tokens(users: list) -> dict:
    """离线签发 access token（不调登录接口，绕开 auth 限流）。"""
    from rest_framework_simplejwt.tokens import RefreshToken

    payload = {}
    for user in users:
        refresh = RefreshToken.for_user(user)
        payload[user.username] = {
            "user_id": user.pk,
            "role": user.role,
            "access": str(refresh.access_token),
        }
    return payload


def cmd_setup(args) -> int:
    db_name = _resolve_db_name(args.db)
    print(f"压测库：{db_name}")
    _bootstrap(db_name, create=True)
    _migrate()

    admin, _ = _ensure_user(
        f"{USER_PREFIX}admin", role="admin", real_name="压测管理员",
        password=PERF_PASSWORD, is_staff=True, is_superuser=True,
    )
    teacher, _ = _ensure_user(
        f"{USER_PREFIX}teacher", role="teacher", real_name="压测教师",
        password=PERF_PASSWORD, subject="",  # 不限学科，才能看到整场考试的作答
    )
    print(f"  · 管理员 {admin.username} / 教师 {teacher.username}")

    students = _ensure_students(args.students)
    print(f"  · 学生 {len(students)} 名（{students[0].username} … {students[-1].username}）")

    questions = _ensure_questions(args.questions)
    paper = _ensure_paper(questions)
    exam = _ensure_exam(paper, teacher)
    print(f"  · 试卷 #{paper.pk} 题量 {paper.question_count} 总分 {paper.total_score}")
    print(f"  · 考试 #{exam.pk} 状态 {exam.status} 时间窗 {exam.start_time} → {exam.end_time}")

    tokens = _issue_tokens([admin, teacher] + students)
    TOKEN_FILE.write_text(json.dumps(tokens, ensure_ascii=False, indent=2), encoding="utf-8")

    meta = {
        "db": db_name,
        "exam_id": exam.pk,
        "paper_id": paper.pk,
        "student_usernames": [item.username for item in students],
        "admin": admin.username,
        "teacher": teacher.username,
        "password": PERF_PASSWORD,
        "token_file": str(TOKEN_FILE),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"  · token 写入 {TOKEN_FILE.name}（{len(tokens)} 个账号）")
    print(f"  · 元数据写入 {META_FILE.name}")
    print("完成。启动服务：")
    print(f"  DB_NAME={db_name} ./.venv/Scripts/python.exe manage.py runserver 127.0.0.1:8022 --noreload")
    return 0


def cmd_users(args) -> int:
    """AC-36 专用：往用户表里灌大批量数据。"""
    db_name = _resolve_db_name(args.db)
    print(f"压测库：{db_name}")
    _bootstrap(db_name, create=False)

    from django.contrib.auth import get_user_model
    from django.contrib.auth.hashers import make_password

    User = get_user_model()
    existing = User.objects.filter(username__startswith=f"{USER_PREFIX}ac36_").count()
    need = max(0, args.count - existing)
    if need == 0:
        print(f"  · 已有 {existing} 条 AC-36 数据，无需新增")
        return 0

    password = make_password(PERF_PASSWORD)  # 只算一次
    batch, created = [], 0
    started = time.time()
    for index in range(existing + 1, existing + need + 1):
        username = f"{USER_PREFIX}ac36_{index:05d}"
        batch.append(
            User(
                username=username,
                email=f"{username}@perf.local",
                real_name=f"批量学生{index:05d}",
                role="student",
                student_no=f"AC36{index:05d}",
                subject="",
                password=password,
            )
        )
        if len(batch) >= 500:
            User.objects.bulk_create(batch, batch_size=500)
            created += len(batch)
            batch.clear()
            print(f"  · 已写入 {created}/{need}")
    if batch:
        User.objects.bulk_create(batch, batch_size=500)
        created += len(batch)

    total = User.objects.count()
    print(f"完成：新增 {created} 条，用时 {time.time() - started:.1f}s，用户表总计 {total} 条")
    return 0


def cmd_cleanup(args) -> int:
    db_name = _resolve_db_name(args.db)
    print(f"压测库：{db_name}")
    _bootstrap(db_name, create=False)

    from django.contrib.auth import get_user_model

    from apps.exam.models import Exam, ExamAttempt
    from apps.papers.models import Paper
    from apps.questions.models import Question

    attempts = ExamAttempt.objects.filter(exam__title__startswith=QUESTION_MARK).count()
    ExamAttempt.objects.filter(exam__title__startswith=QUESTION_MARK).delete()
    exams = Exam.objects.filter(title__startswith=QUESTION_MARK).delete()
    papers = Paper.objects.filter(title__startswith=QUESTION_MARK).delete()
    # PaperQuestion 随试卷级联删除，题目这时候才能删（question 是 PROTECT）
    questions = Question.objects.filter(stem__startswith=QUESTION_MARK).delete()
    users = get_user_model().objects.filter(username__startswith=USER_PREFIX).delete()
    print(f"  · 作答 {attempts} / 考试 {exams} / 试卷 {papers} / 题目 {questions} / 用户 {users}")

    for path in (META_FILE, TOKEN_FILE):
        if path.exists():
            path.unlink()
            print(f"  · 删除 {path.name}")

    if args.drop_db:
        _drop_database(db_name)
        print(f"  · 已 DROP DATABASE `{db_name}`")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="模块 11 性能测试数据准备")
    parser.add_argument("--db", help=f"压测库名（默认 {PERF_DB_DEFAULT}）")
    sub = parser.add_subparsers(dest="command")

    setup = sub.add_parser("setup", help="建库 + 迁移 + 建压测数据")
    setup.add_argument("--students", type=int, default=30, help="学生数量，默认 30")
    setup.add_argument("--questions", type=int, default=20, help="单选题数量，默认 20")
    setup.set_defaults(func=cmd_setup)

    users = sub.add_parser("users", help="AC-36：灌大批量用户")
    users.add_argument("--count", type=int, default=10000, help="AC-36 用户总数，默认 10000")
    users.set_defaults(func=cmd_users)

    cleanup = sub.add_parser("cleanup", help="清理压测数据")
    cleanup.add_argument("--drop-db", action="store_true", help="连库一起删")
    cleanup.set_defaults(func=cmd_cleanup)

    args = parser.parse_args()
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
