"""模块 11 · Locust 压测脚本。

三个场景，用 ``--tags`` 或「命令行末尾写类名」二选一挑选：

    ExamSubmitUser       并发交卷     学生：开考 → 逐题暂存 → 交卷 → 再开考
    ExamStartBurstUser   开考读峰值   学生：开考后集中拉卷面 / 心跳 / 考试详情
    ExamGradingUser      批量判分     教师：把已交卷的作答跑一遍客观题判分

跑法（Git Bash，项目根目录）::

    # 单场景：类名写在最后
    ./.venv/Scripts/locust.exe -f scripts/locustfile.py ExamSubmitUser \
        --headless -u 20 -r 5 -t 30s --only-summary --host http://127.0.0.1:8024

    # 也可以用标签
    ./.venv/Scripts/locust.exe -f scripts/locustfile.py --tags submit \
        --headless -u 20 -r 5 -t 30s --only-summary --host http://127.0.0.1:8024

前置条件：

  1. ``scripts/perf_seed.py setup`` 已跑过（生成 ``.perf_meta.json`` / ``.perf_tokens.json``）；
  2. 服务指向压测库（``DB_NAME=online_exam_perf``），**不是**演示库；
  3. ``AI_PROVIDER=mock`` —— 本脚本只发客观题请求、判分固定 ``with_ai=false``，
     正常情况下不会触发任何大模型调用；这一条是双保险，防止将来有人把
     ``with_ai`` 默认值改掉之后压测悄悄烧钱。

账号来源（**不硬编码密码**，按优先级）：

  1. ``.perf_tokens.json``：seed 离线签发的 access token，直接复用，不打登录接口。
     登录接口有 ``auth`` 限流 30/min（按 IP），压测时所有虚拟用户都来自 127.0.0.1，
     逐用户登录会在第 30 次之后全军覆没 —— 那不是系统慢，是限流。
  2. ``PERF_STUDENTS``（逗号分隔用户名）+ ``PERF_PASSWORD``：走真实登录接口。

其他环境变量：

  ``PERF_EXAM_ID``   考试 ID（缺省读 meta）
  ``PERF_API_PREFIX`` 接口前缀，默认 ``/api/v1``（为了将来换版本号不用改脚本）
"""

from __future__ import annotations

import itertools
import json
import os
import random
from pathlib import Path

from locust import HttpUser, between, events, tag, task

BASE_DIR = Path(__file__).resolve().parent.parent
META_FILE = BASE_DIR / "scripts" / ".perf_meta.json"
TOKEN_FILE = BASE_DIR / "scripts" / ".perf_tokens.json"

API = os.environ.get("PERF_API_PREFIX", "/api/v1").rstrip("/")


# ----------------------------------------------------------------------
# 环境与账号
# ----------------------------------------------------------------------
def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


META = _load_json(META_FILE)
TOKENS = _load_json(TOKEN_FILE)

EXAM_ID = int(os.environ.get("PERF_EXAM_ID") or META.get("exam_id") or 0)
PASSWORD = os.environ.get("PERF_PASSWORD") or META.get("password") or ""


def _credential_pool() -> list[dict]:
    """按优先级拼出可用账号池：[{"username", "access"} 或 {"username", "password"}]。"""
    pool: list[dict] = []

    usernames = [x.strip() for x in (os.environ.get("PERF_STUDENTS") or "").split(",") if x.strip()]
    if not usernames:
        usernames = META.get("student_usernames") or []

    for name in usernames:
        entry = TOKENS.get(name) or {}
        if entry.get("access"):
            pool.append({"username": name, "access": entry["access"]})
        elif PASSWORD:
            pool.append({"username": name, "password": PASSWORD})

    return pool


# 导入阶段不抛异常：locust 的 --list 也需要能加载脚本，
# 账号缺失留到 test_start 里报（那时才真的开始压）
STUDENT_POOL = _credential_pool()
_POOL_CURSOR = itertools.count()


def _next_student() -> dict:
    return STUDENT_POOL[next(_POOL_CURSOR) % len(STUDENT_POOL)]


def _teacher_login(client) -> str:
    """教师账号：优先用 seed 签发的 token，否则走登录接口（只有一个账号，不会撞限流）。"""
    name = os.environ.get("PERF_TEACHER") or META.get("teacher") or ""
    token = (TOKENS.get(name) or {}).get("access")
    if token:
        return token
    if not (name and PASSWORD):
        raise RuntimeError("缺少教师账号：请跑 seed 脚本，或设置 PERF_TEACHER + PERF_PASSWORD")
    resp = client.post(
        f"{API}/auth/login/",
        json={"username": name, "password": PASSWORD},
        name="POST /auth/login/ (教师)",
    )
    return resp.json()["data"]["access"]


# ----------------------------------------------------------------------
# 公共基类：认证 + 开考
# ----------------------------------------------------------------------
class _ExamUser(HttpUser):
    abstract = True
    wait_time = between(0.5, 2.0)

    # 子类在 on_start 里赋值
    access_token: str = ""
    exam_id: int = 0
    attempt_id: int = 0
    questions: list = []

    def login(self) -> bool:
        """拿到 access token。有种子 token 就零成本复用。"""
        credential = _next_student()
        if credential.get("access"):
            self.access_token = credential["access"]
            return True
        resp = self.client.post(
            f"{API}/auth/login/",
            json={"username": credential["username"], "password": credential["password"]},
            name="POST /auth/login/",
        )
        if resp.status_code != 200:
            resp.failure(f"登录失败：{resp.status_code} {resp.text[:120]}")
            return False
        self.access_token = resp.json()["data"]["access"]
        return True

    @property
    def auth(self) -> dict:
        return {"Authorization": f"Bearer {self.access_token}"}

    def resolve_exam_id(self) -> int:
        """没给 PERF_EXAM_ID 时，用学生身份从「我的考试」列表里挑第一场。"""
        if EXAM_ID:
            return EXAM_ID
        resp = self.client.get(f"{API}/exams/", headers=self.auth, name="GET /exams/ (定位考试)")
        if resp.status_code != 200:
            return 0
        rows = (resp.json().get("data") or {}).get("results") or []
        return int(rows[0]["id"]) if rows else 0

    def start_exam(self) -> bool:
        """开考（有进行中的作答会「续考」，天然幂等）。成功后记下 attempt 与题目清单。"""
        if not self.exam_id:
            return False
        resp = self.client.post(
            f"{API}/exams/{self.exam_id}/start/",
            headers=self.auth,
            json={"confirm": True},
            name="POST /exams/[id]/start/",
        )
        if resp.status_code != 200:
            resp.failure(f"开考失败：{resp.status_code} {resp.text[:120]}")
            return False
        data = resp.json().get("data") or {}
        self.attempt_id = int((data.get("attempt") or {}).get("id") or 0)
        self.questions = [
            int(item["paper_question_id"]) for item in (data.get("questions") or [])
        ]
        if not self.attempt_id:
            resp.failure("开考返回里没有 attempt.id")
            return False
        return True

    # ---- 通用动作 ----
    def save_some_answers(self, count: int = 3) -> None:
        """批量暂存若干道客观题答案（走 POST /attempts/{id}/answers/）。"""
        if not self.questions:
            return
        picks = random.sample(self.questions, min(count, len(self.questions)))
        items = [
            {"paper_question_id": pid, "selected_labels": random.choice(["A", "B", "C", "D"]),
             "time_spent_seconds": random.randint(5, 40)}
            for pid in picks
        ]
        with self.client.post(
            f"{API}/attempts/{self.attempt_id}/answers/",
            headers=self.auth,
            json={"items": items},
            name="POST /attempts/[id]/answers/",
            catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"暂存失败：{resp.status_code} {resp.text[:120]}")
            elif (resp.json() or {}).get("code") != 0:
                resp.failure(f"暂存业务错误：{resp.text[:120]}")


# ----------------------------------------------------------------------
# 场景 A：并发交卷
# ----------------------------------------------------------------------
@tag("submit", "write")
class ExamSubmitUser(_ExamUser):
    """模拟「考试铃响，全班同时点击交卷」。

    一个循环 = 开考 → 暂存两轮 → 交卷 → 立刻开考下一轮（同一学生反复作答，
    考试配置了 max_attempts=200 就是为了支撑这种循环）。
    写操作集中在 ``Answer`` 的 update_or_create 与 ``ExamAttempt`` 的收尾 UPDATE。
    """

    weight = 3

    def on_start(self):
        if not self.login():
            return
        self.exam_id = self.resolve_exam_id()
        self.start_exam()

    @task(4)
    def answer_and_submit(self):
        if not self.attempt_id:
            self.start_exam()
            return

        self.save_some_answers(count=3)

        items = [
            {"paper_question_id": pid, "selected_labels": random.choice(["A", "B", "C", "D"])}
            for pid in random.sample(self.questions, min(5, len(self.questions))) if self.questions
        ]
        with self.client.post(
            f"{API}/attempts/{self.attempt_id}/submit/",
            headers=self.auth,
            json={"reason": "manual", "items": items},
            name="POST /attempts/[id]/submit/",
            catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"交卷失败：{resp.status_code} {resp.text[:120]}")
            elif (resp.json() or {}).get("code") != 0:
                resp.failure(f"交卷业务错误：{resp.text[:120]}")

        # 交完立刻开下一轮，保持"持续有人交卷"的稳态
        self.start_exam()

    @task(1)
    def heartbeat(self):
        if not self.attempt_id:
            return
        self.client.post(
            f"{API}/attempts/{self.attempt_id}/heartbeat/",
            headers=self.auth,
            json={"client_remaining": random.randint(600, 7000)},
            name="POST /attempts/[id]/heartbeat/",
        )


# ----------------------------------------------------------------------
# 场景 B：开考瞬间的读峰值
# ----------------------------------------------------------------------
@tag("startburst", "read")
class ExamStartBurstUser(_ExamUser):
    """模拟「开考瞬间，几十上百个学生同时刷新页面拉卷子」。

    读密集：``GET /attempts/{id}/paper/`` 会重建整份卷面（含选项、已存答案、
    乱序计算），是本场景最重的读；加上考试详情与心跳构成典型的开考流量。
    """

    weight = 3

    def on_start(self):
        if not self.login():
            return
        self.exam_id = self.resolve_exam_id()
        self.start_exam()

    @task(6)
    def fetch_paper(self):
        if not self.attempt_id:
            self.start_exam()
            return
        with self.client.get(
            f"{API}/attempts/{self.attempt_id}/paper/",
            headers=self.auth,
            name="GET /attempts/[id]/paper/",
            catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"拉卷面失败：{resp.status_code} {resp.text[:120]}")
            else:
                data = (resp.json() or {}).get("data") or {}
                if not data.get("questions"):
                    resp.failure("卷面为空（questions 缺失）")

    @task(2)
    def fetch_exam_detail(self):
        if not self.exam_id:
            return
        self.client.get(
            f"{API}/exams/{self.exam_id}/",
            headers=self.auth,
            name="GET /exams/[id]/",
        )

    @task(2)
    def heartbeat(self):
        if not self.attempt_id:
            return
        self.client.post(
            f"{API}/attempts/{self.attempt_id}/heartbeat/",
            headers=self.auth,
            json={},
            name="POST /attempts/[id]/heartbeat/",
        )

    @task(1)
    def list_my_attempts(self):
        self.client.get(
            f"{API}/attempts/",
            headers=self.auth,
            name="GET /attempts/",
        )


# ----------------------------------------------------------------------
# 场景 C：批量判分（教师侧）
# ----------------------------------------------------------------------
@tag("grading")
class ExamGradingUser(HttpUser):
    """教师侧批量判分：对已交卷的作答反复跑 ``grade_attempt(with_ai=False)``。

    为什么单独一档：模块四的交卷只把状态推到「已交卷」，判分是教师/后台另起的动作。
    想验证 ``GradingService.recalc`` 会不会成为热点，就得单独压它。
    ``with_ai`` 固定 false：带 AI 会真的调大模型，既慢又花钱，也不可能复现。
    """

    weight = 1
    wait_time = between(1, 3)

    def on_start(self):
        self.access_token = _teacher_login(self.client)
        self.exam_id = EXAM_ID
        self.attempt_ids: list[int] = []
        if not self.exam_id:
            return
        resp = self.client.get(
            f"{API}/grading/queue/",
            headers={"Authorization": f"Bearer {self.access_token}"},
            params={"exam_id": self.exam_id, "only_pending": "false"},
            name="GET /grading/queue/",
        )
        if resp.status_code == 200:
            rows = (resp.json().get("data") or {}).get("results") or []
            self.attempt_ids = [int(row["attempt_id"]) for row in rows]

    @task
    def grade_one(self):
        if not self.attempt_ids:
            return
        attempt_id = random.choice(self.attempt_ids)
        with self.client.post(
            f"{API}/grading/attempts/{attempt_id}/grade/",
            headers={"Authorization": f"Bearer {self.access_token}"},
            json={"with_ai": False, "force": True},
            name="POST /grading/attempts/[id]/grade/",
            catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"判分失败：{resp.status_code} {resp.text[:120]}")
            elif (resp.json() or {}).get("code") != 0:
                resp.failure(f"判分业务错误：{resp.text[:120]}")


# ----------------------------------------------------------------------
# 启动前的"防烧钱"自检
# ----------------------------------------------------------------------
@events.test_start.add_listener
def _guard_ai(environment, **_kwargs):
    provider = (os.environ.get("AI_PROVIDER") or "").strip().lower()
    if provider and provider != "mock":
        print(
            f"[警告] AI_PROVIDER={provider!r}：判分场景固定 with_ai=false，"
            f"理论上不会调大模型；但仍建议用 AI_PROVIDER=mock 起服务，避免意外计费。"
        )
    if not STUDENT_POOL:
        raise RuntimeError("账号池为空，压测无法开始")
    print(f"[perf] 账号池 {len(STUDENT_POOL)} 个，考试 ID={EXAM_ID or '（运行时探测）'}")
