# -*- coding: utf-8 -*-
"""模板内联 JS 语法检查。

Django 模板里的 ``<script>`` 混着 ``{{ }}`` / ``{% %}`` 插值，直接对 .html
跑 ``node --check`` 会误报。所以这里先把页面渲染成真实 HTML，再把不含 ``src``
的 ``<script>`` 块逐个抽出来交给 node 校验——**检查的是浏览器实际会执行的代码**，
插值渲染错了也能一并暴露出来。

用法：
    python scripts/check_inline_js.py                 # 检查全部页面
    python scripts/check_inline_js.py /papers/        # 只检查指定页面

需要 node 在 PATH 中（或设置环境变量 ``NODE_BIN`` 指向可执行文件）。
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "OnlineExam.settings.development")
django.setup()

from django.test import Client  # noqa: E402

from apps.exam.models import Exam  # noqa: E402
from apps.papers.models import Paper  # noqa: E402
from apps.users.models import User  # noqa: E402

SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)


def node_binary() -> str:
    """优先用 NODE_BIN，其次找 PATH 里的 node。"""
    candidate = os.environ.get("NODE_BIN") or shutil.which("node")
    if not candidate:
        print("找不到 node，请设置环境变量 NODE_BIN 指向 node 可执行文件")
        sys.exit(2)
    return candidate


def pages_to_check() -> list:
    """默认检查所有「有内联 JS」的页面。

    返回 ``[(路径, 用哪个身份访问)]``。身份很重要：答题页只有学生能进，
    考试列表只有教师能进；拿错身份会全部 302，一个脚本块都检不到。
    """
    if len(sys.argv) > 1:
        return [(path, "teacher") for path in sys.argv[1:]]

    pages = [
        ("/", "teacher"),
        ("/login/", "anon"),
        ("/register/", "anon"),
        ("/profile/", "teacher"),
        ("/question-bank/", "teacher"),
        ("/question-bank/new/", "teacher"),
        ("/question-bank/ai-generate/", "teacher"),
        ("/knowledge-points/", "teacher"),
        ("/papers/", "teacher"),
        ("/papers/new/", "teacher"),
        ("/exams/", "teacher"),
        ("/my-exams/", "student"),
        ("/grading/", "teacher"),
        ("/my-wrong-book/", "student"),
        ("/analytics/", "teacher"),
        ("/analytics/me/", "student"),
        ("/practice/", "student"),
        ("/proctoring/", "teacher"),
        ("/classrooms/", "teacher"),
    ]

    # 需要真实数据的页面：有卷子才查组卷工作台，有考试才查监控 / 答题 / 结果
    paper = Paper.objects.order_by("id").first()
    if paper:
        pages.append((f"/papers/{paper.id}/compose/", "teacher"))
        pages.append((f"/question-bank/{paper.id}/edit/", "teacher"))

    exam = Exam.objects.order_by("id").first()
    if exam:
        pages.append((f"/exams/{exam.id}/monitor/", "teacher"))
        pages.append((f"/exams/{exam.id}/take/", "student"))
        pages.append((f"/exams/{exam.id}/result/", "student"))
        pages.append((f"/grading/exams/{exam.id}/scoreboard/", "teacher"))

    # 教师看某个学生的学情（学情分析模块）
    student_user = User.objects.filter(role="student").order_by("id").first()
    if student_user:
        pages.append((f"/analytics/students/{student_user.id}/", "teacher"))

    # 证据链页面（作弊检测模块）：需要一份真实作答
    from apps.exam.models import ExamAttempt

    attempt = ExamAttempt.objects.order_by("id").first()
    if attempt:
        pages.append((f"/proctoring/attempts/{attempt.id}/evidence/", "teacher"))

    # 班级详情页：需要真实班级
    from apps.classroom.models import Classroom

    classroom = Classroom.objects.order_by("id").first()
    if classroom:
        pages.append((f"/classrooms/{classroom.id}/", "teacher"))
    return pages


def main() -> int:
    node = node_binary()

    # 页面按角色守卫，这里各准备一个身份
    teacher = (
        User.objects.filter(role="teacher").order_by("id").first()
        or User.objects.filter(is_superuser=True).first()
    )
    if teacher is None:
        print("数据库里没有教师账号，请先执行 python manage.py seed_users")
        return 2

    clients = {
        "teacher": Client(),
        "student": Client(),
        "anon": Client(),
    }
    clients["teacher"].force_login(teacher)

    student = User.objects.filter(role="student").order_by("id").first()
    if student:
        clients["student"].force_login(student)

    checked = 0
    failed = 0
    skipped = []
    for path, role in pages_to_check():
        if role == "student" and student is None:
            skipped.append(f"{path} (无学生账号)")
            continue
        client = clients[role]
        response = client.get(path)
        if response.status_code in (301, 302):
            # 登录状态下被弹回首页的页面（登录/注册），换匿名再取一次
            response = clients["anon"].get(path)
        if response.status_code != 200:
            skipped.append(f"{path} ({response.status_code})")
            continue

        html = response.content.decode("utf-8", "ignore")
        for index, block in enumerate(SCRIPT_RE.findall(html)):
            code = block.strip()
            if len(code) < 20:  # 空块或只有一行配置的，跳过
                continue
            checked += 1
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                             encoding="utf-8") as handle:
                handle.write(code)
                tmp = handle.name
            try:
                result = subprocess.run([node, "--check", tmp],
                                        capture_output=True, text=True)
            finally:
                os.unlink(tmp)

            label = f"{path} [{role}] 脚本块#{index}"
            if result.returncode == 0:
                print(f"[PASS] {label}  ({len(code)} 字符)")
            else:
                failed += 1
                print(f"[FAIL] {label}")
                print(result.stderr.strip()[:800])

    if skipped:
        print(f"\n[SKIP] {len(skipped)} 个页面未渲染成功：{'、'.join(skipped)}")

    print(f"\n检查了 {checked} 个脚本块，失败 {failed} 个")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
