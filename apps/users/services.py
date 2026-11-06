"""
用户模块的领域服务。

凡「一步以上、需要事务」的业务动作都放这里，视图只负责收参数、返响应。
"""

import logging
import secrets

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from apps.common.constants import Role
from apps.common.exceptions import BusinessError

logger = logging.getLogger("apps.users.services")

User = get_user_model()

# ------------------------------------------------------------------
# 随机密码字母表：剔除易混淆字符 0 / O / 1 / l / I（PRD §4.5）
# ------------------------------------------------------------------
PASSWORD_LOWER = "abcdefghijkmnopqrstuvwxyz"        # 去掉 l
PASSWORD_UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"         # 去掉 I、O
PASSWORD_DIGITS = "23456789"                        # 去掉 0、1
PASSWORD_ALPHABET = PASSWORD_LOWER + PASSWORD_UPPER + PASSWORD_DIGITS

# 随机生成的最大重试次数。
# 注意：这里**没有**「固定兜底密码」——兜底也必须是随机的，
# 否则两个用户可能拿到同一个默认密码（P3-2：重置密码绝不能重复发同一个口令）。
_PASSWORD_MAX_ATTEMPTS = 8

# ------------------------------------------------------------------
# 学号 / 工号派发规则
#   学生：{4 位年级}{4 位顺位}，如 2026 级第 46 人 → 20260046
#   教师：T{4 位顺位}，如 T2004
# 顺位一律「同类已有最大值 + 1」，不回收已用号码（学号是身份标识，不复用）。
# ------------------------------------------------------------------
NO_SEQ_WIDTH = 4
EMPLOYEE_NO_PREFIX = "T"


class UserService:
    """用户注册 / 改密 / 登录痕迹记录 / 管理端账号治理。"""

    # ------------------------------------------------------------------
    # 学号 / 工号派发
    # ------------------------------------------------------------------
    @staticmethod
    def _next_seq(prefix: str, width: int = NO_SEQ_WIDTH) -> str:
        """在 ``prefix`` 下找下一个未被占用的顺位号。

        不用「最大值 +1」直接返回，而是**再校验一次是否已被占用**，
        既兼容历史数据里顺位不连续的情况，也避免与手工录入的号撞车。
        """
        used = set(
            User.objects.filter(student_no__startswith=prefix)
            .values_list("student_no", flat=True)
        )
        seq = 0
        for no in used:
            tail = no[len(prefix):]
            if tail.isdigit():
                seq = max(seq, int(tail))
        seq += 1
        while f"{prefix}{seq:0{width}d}" in used:
            seq += 1
        return f"{prefix}{seq:0{width}d}"

    @staticmethod
    def generate_student_no(grade_year) -> str:
        """按年级派发学号：2026 级 → 20260001 / 20260002 …

        ``grade_year`` 取 4 位年份（如 ``2026``，也接受 ``"2026级"``）。
        """
        year = str(grade_year or "").strip()
        if year.endswith("级"):
            year = year[:-1]
        if not year.isdigit() or len(year) != 4:
            raise BusinessError("年级格式不正确，应为 4 位年份（如 2026）", code=10007)
        return UserService._next_seq(year)

    @staticmethod
    def generate_employee_no() -> str:
        """派发教师工号：T + 顺位（现有 T1001 / T2001~T2003 → 下一个 T2004）。"""
        return UserService._next_seq(EMPLOYEE_NO_PREFIX)

    @staticmethod
    def student_no_matches_grade(student_no, grade_year) -> bool:
        """手填学号时，前 4 位是否与所选年级一致。

        防的是「学号 20260001 却把年级选成 2025」这类脏数据——年级虽然不单独存字段，
        但学号前缀就是年级，两者不一致等于数据自相矛盾。

        非「4 位数字开头」的学号（历史手工号、教师工号 Txxxx）视为不适用，放行。
        """
        no = (student_no or "").strip()
        if not no or len(no) < 4 or not no[:4].isdigit():
            return True
        return no[:4] == str(grade_year or "").strip()

    @staticmethod
    @transaction.atomic
    def register(*, username: str, password: str, email: str, role: str = Role.STUDENT,
                 real_name: str = "", student_no: str = "", phone: str = "",
                 school: str = "", grade_year=None) -> "User":
        """注册新用户。

        本服务**不做**「只能注册学生」的硬限制：管理端建号复用同一方法。
        注册收敛的边界收在公开入口（``UserRegisterSerializer`` / ``RegisterForm``）。

        ``student_no`` 不传时按角色自动派发：学生按 ``grade_year`` 派学号，
        教师派工号——注册入口不再让用户手填身份编号。
        """
        if role == Role.ADMIN:
            raise BusinessError("不允许通过注册接口创建管理员账号", code=10003, status_code=403)

        if User.objects.filter(username__iexact=username).exists():
            raise BusinessError("该用户名已被占用", code=10001)
        if email and User.objects.filter(email__iexact=email).exists():
            raise BusinessError("该邮箱已被注册", code=10002)

        student_no = (student_no or "").strip()
        if not student_no:
            if role == Role.STUDENT:
                if not grade_year:
                    raise BusinessError("学生注册需要选择年级，用于派发学号", code=10007)
                student_no = UserService.generate_student_no(grade_year)
            else:
                student_no = UserService.generate_employee_no()
        if User.objects.filter(student_no__iexact=student_no).exists():
            raise BusinessError("该学号 / 工号已被使用", code=10004)

        user = User(
            username=username,
            email=email,
            role=role,
            real_name=real_name,
            student_no=student_no,
            phone=phone,
            school=school,
        )
        user.set_password(password)
        user.save()

        logger.info("新用户注册成功: %s (%s)", user.username, user.get_role_display())
        return user

    @staticmethod
    @transaction.atomic
    def change_password(user, old_password: str, new_password: str) -> None:
        """修改密码。改完把 refresh token 全部拉黑，强制其它端重新登录。"""
        if not user.check_password(old_password):
            raise BusinessError("原密码不正确", code=10005)

        user.set_password(new_password)
        user.save(update_fields=["password"])
        UserService._blacklist_refresh_tokens(user)

    # ------------------------------------------------------------------
    # 管理端：重置密码（D-3）
    # ------------------------------------------------------------------
    @staticmethod
    def generate_password(length: int = 12) -> str:
        """生成密码学安全的随机密码。

        规则（PRD §4.5）：12 位、大小写字母 + 数字、剔除易混淆字符、
        且**保证**至少各含一个小写、大写、数字（否则可能被 MinimumLength 之外
        的口径挑刺，也不利于人工转述）。
        """
        length = max(int(length or 12), 9)
        rng = secrets.SystemRandom()

        chars = [
            rng.choice(PASSWORD_LOWER),
            rng.choice(PASSWORD_UPPER),
            rng.choice(PASSWORD_DIGITS),
        ]
        chars.extend(rng.choice(PASSWORD_ALPHABET) for _ in range(length - len(chars)))
        rng.shuffle(chars)
        return "".join(chars)

    @staticmethod
    @transaction.atomic
    def admin_reset_password(user, new_password: str = None) -> str:
        """管理员重置某账号的密码，返回**明文**（仅此一次，调用方负责只回一次）。

        ``new_password`` 缺省 → 服务端随机生成；有值 → 必须是管理员手输的强密码。
        两条路径**都要**过 ``AUTH_PASSWORD_VALIDATORS``（唯一权威口径）。

        Raises:
            ValidationError: 密码不满足 ``settings.AUTH_PASSWORD_VALIDATORS``。
        """
        plain = (new_password or "").strip()
        if plain:
            validate_password(plain, user=user)
        else:
            plain = UserService._generate_valid_password(user)

        user.set_password(plain)
        user.save(update_fields=["password"])
        UserService._blacklist_refresh_tokens(user)

        # 只记「谁被重置了」，绝不记明文（PRD §4.5：不落日志）
        logger.info("管理员重置密码成功: user=%s", user.username)
        return plain

    @staticmethod
    def _generate_valid_password(user) -> str:
        """生成一个能通过当前密码策略的**每次都不同**的随机密码。

        策略：用密码学安全随机源反复生成，直到通过 ``AUTH_PASSWORD_VALIDATORS``。
        连续 ``_PASSWORD_MAX_ATTEMPTS`` 次都不过（正常配置下概率可忽略，
        通常意味着密码策略被改到极严，例如禁用全部常用字符集）时，
        **抛错让管理员重试或手填**，绝不静默返回一个固定值——
        固定兜底会让「重置密码」在批量操作下给多个用户发同一个口令（P3-2）。

        Raises:
            BusinessError: code=10013，无法生成符合策略的随机密码。
        """
        last_error = None
        for _ in range(_PASSWORD_MAX_ATTEMPTS):
            candidate = UserService.generate_password()
            try:
                validate_password(candidate, user=user)
                return candidate
            except ValidationError as exc:  # 极低概率：例如与用户名过于相似
                last_error = exc
                continue

        logger.error(
            "连续 %s 次随机密码均未通过策略校验，放弃自动生成: user=%s | last=%s",
            _PASSWORD_MAX_ATTEMPTS, getattr(user, "username", "-"), last_error,
        )
        raise BusinessError(
            "无法生成符合密码策略的随机密码，请重试或手动指定新密码", code=10013
        )

    @staticmethod
    def _blacklist_refresh_tokens(user) -> None:
        """把该用户的 refresh token 全部拉黑：所有已登录的 JWT 会话立即失效。"""
        try:
            from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
            from rest_framework_simplejwt.tokens import RefreshToken

            for token in OutstandingToken.objects.filter(user=user):
                try:
                    RefreshToken(token.token).blacklist()
                except Exception:  # 已过期 / 已拉黑的 token 直接跳过
                    continue
        except Exception as exc:  # 未启用 token_blacklist 时不影响主流程
            logger.warning("拉黑历史 token 失败（可忽略）: %s", exc)

    # ------------------------------------------------------------------
    # 管理端：账号治理护栏（U-2 / AC-18）
    # ------------------------------------------------------------------
    @staticmethod
    def assert_admin_survives(target, role: str = None, is_active: bool = None) -> None:
        """护栏：不允许把「最后一个启用状态的管理员」停用或降级。

        口径（U-2）：管理员 = ``role == 'admin'`` **或** ``is_superuser=True``；
        「启用」= ``is_active=True``。命中数 ≤ 1 且本次操作会让它变成 0 时拒绝。

        幂等：目标本来就不是「启用管理员」时直接放行，不影响任何普通账号的启停。

        Raises:
            BusinessError: code=10012，系统至少保留一个启用的管理员。
        """
        new_role = target.role if role is None else role
        new_active = target.is_active if is_active is None else is_active

        if not UserService._is_active_admin(target):
            return
        if bool(new_active) and (target.is_superuser or new_role == Role.ADMIN):
            return

        remaining = (
            User.objects.filter(is_active=True)
            .filter(Q(role=Role.ADMIN) | Q(is_superuser=True))
            .exclude(pk=target.pk)
            .count()
        )
        if remaining == 0:
            raise BusinessError("系统至少保留一个启用的管理员", code=10012)

    @staticmethod
    def _is_active_admin(user) -> bool:
        """是否「启用状态的管理员」。"""
        if not getattr(user, "is_active", False):
            return False
        return bool(getattr(user, "is_superuser", False) or getattr(user, "role", None) == Role.ADMIN)

    @staticmethod
    def record_login(user, ip: str = None) -> None:
        """记录登录 IP，用于「上次登录地点」这类安全提示。"""
        if ip:
            User.objects.filter(pk=user.pk).update(last_login_ip=ip)


# ----------------------------------------------------------------------
# 首页工作台
# ----------------------------------------------------------------------
class HomeDashboardService:
    """首页工作台数据。

    设计原则：**导航负责「去哪里」，首页负责「现在要做什么」**。
    首页不再罗列功能入口（那是导航的职责），只给三类信息：

      * ``stats``  —— 四个关键数字（点进去能到对应列表页）
      * ``todos``  —— 真正需要处理的事（待批改、即将开始、进行中的作答）
      * ``recent_exams`` —— 最近的考试动态

    全部在服务端一次算完（首页只发 1 个请求），且只做轻量 count/limit 查询，
    不在首页跑学情/推荐这类重计算（那些留在各自页面）。
    """

    @staticmethod
    def build(user) -> dict:
        role = getattr(user, "role", None)
        if role == Role.TEACHER:
            return HomeDashboardService._teacher(user)
        if role == Role.STUDENT:
            return HomeDashboardService._student(user)
        return {}

    # ------------------------------------------------------------------
    @staticmethod
    def _teacher(user) -> dict:
        from apps.classroom.models import Classroom
        from apps.common.constants import AttemptStatus, ExamStatus
        from apps.exam.models import Exam, ExamAttempt

        now = timezone.now()
        # 开考后 exam.status 会被推进成 ongoing（落库），两态都要算「我的考试」
        exams = Exam.objects.filter(
            status__in=[ExamStatus.PUBLISHED, ExamStatus.ONGOING],
        )
        # 学科隔离：有任教学科的教师只统计自己学科
        if getattr(user, "subject", ""):
            exams = exams.filter(subject=user.subject)

        in_window = Q(end_time__isnull=True) | Q(end_time__gte=now)
        # 未设开始时间 = 不限时，也算「进行中」
        started = Q(start_time__isnull=True) | Q(start_time__lte=now)
        ongoing_qs = exams.filter(started).filter(in_window)
        upcoming_qs = exams.filter(start_time__gt=now).order_by("start_time")
        pending_qs = ExamAttempt.objects.filter(
            status__in=[AttemptStatus.SUBMITTED, AttemptStatus.REVIEWING],
            exam__in=exams,
        )
        classrooms = Classroom.objects.manageable_by(user).active()

        pending_count = pending_qs.count()
        ongoing_count = ongoing_qs.count()
        upcoming = list(upcoming_qs[:3])
        recent = list(
            exams.with_counts().order_by("-start_time")[:5]
        )
        status_labels = dict(ExamStatus.choices)

        stats = [
            {"label": "待批改", "value": pending_count, "unit": "份",
             "icon": "bi-clipboard-check", "url": "/grading/", "tone": "warning"},
            {"label": "进行中考试", "value": ongoing_count, "unit": "场",
             "icon": "bi-broadcast", "url": "/exams/", "tone": "danger"},
            {"label": "即将开始", "value": upcoming_qs.count(), "unit": "场",
             "icon": "bi-clock-history", "url": "/exams/", "tone": "info"},
            {"label": "我的班级", "value": classrooms.count(), "unit": "个",
             "icon": "bi-people", "url": "/classrooms/", "tone": "success"},
        ]

        todos = []
        if pending_count:
            todos.append({
                "badge": "待批改", "tone": "warning",
                "title": f"有 {pending_count} 份作答等待批改",
                "desc": "主观题需要人工复核或确认 AI 建议分",
                "url": "/grading/",
            })
        for exam in ongoing_qs.select_related("paper")[:2]:
            todos.append({
                "badge": "进行中", "tone": "danger",
                "title": exam.title,
                "desc": f"截止 {exam.end_time:%m-%d %H:%M}" if exam.end_time else "无截止时间",
                "url": f"/exams/{exam.id}/monitor/",
            })
        for exam in upcoming:
            todos.append({
                "badge": "待开始", "tone": "info",
                "title": exam.title,
                "desc": f"开始 {exam.start_time:%m-%d %H:%M}" if exam.start_time else "待定",
                "url": "/exams/",
            })

        return {
            "role": "teacher",
            "stats": stats,
            "todos": todos[:5],
            "recent_exams": [
                {
                    "id": exam.id,
                    "title": exam.title,
                    "status_display": status_labels.get(
                        exam.computed_status(), "—",
                    ),
                    "subject": exam.subject or "—",
                    "start_time": exam.start_time,
                    "end_time": exam.end_time,
                    "attempt_count": getattr(exam, "attempt_count", None),
                    "url": f"/exams/{exam.id}/monitor/",
                }
                for exam in recent
            ],
            "actions": [
                {"title": "新建题目", "url": "/question-bank/new/", "icon": "bi-plus-circle"},
                {"title": "智能组卷", "url": "/papers/new/", "icon": "bi-magic"},
                {"title": "发布考试", "url": "/exams/", "icon": "bi-send"},
            ],
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _student(user) -> dict:
        from apps.classroom.models import Classroom
        from apps.common.constants import AttemptStatus
        from apps.exam.models import Exam, ExamAttempt

        now = timezone.now()
        exams = Exam.objects.for_student(user)

        in_progress = (
            ExamAttempt.objects.in_progress().filter(student=user)
            .select_related("exam").first()
        )
        in_window = Q(end_time__isnull=True) | Q(end_time__gte=now)
        started = Q(start_time__isnull=True) | Q(start_time__lte=now)
        ongoing = (
            exams.filter(started).filter(in_window)
            .order_by("start_time").first()
        )
        upcoming = exams.filter(start_time__gt=now).order_by("start_time").first()
        latest = (
            ExamAttempt.objects.filter(
                student=user,
                status__in=[AttemptStatus.AUTO_GRADED, AttemptStatus.REVIEWING,
                            AttemptStatus.GRADED],
            )
            .select_related("exam", "exam__paper")
            .order_by("-submitted_at").first()
        )
        classrooms = Classroom.objects.visible_to(user).active()

        taken = ExamAttempt.objects.filter(
            student=user,
            status__in=[AttemptStatus.SUBMITTED, AttemptStatus.AUTO_GRADED,
                        AttemptStatus.REVIEWING, AttemptStatus.GRADED,
                        AttemptStatus.TIMEOUT],
        ).count()

        stats = [
            {"label": "待考", "value": exams.filter(start_time__gt=now).count(), "unit": "场",
             "icon": "bi-calendar-event", "url": "/my-exams/", "tone": "info"},
            {"label": "进行中", "value": 1 if in_progress else 0, "unit": "场",
             "icon": "bi-pencil-square", "url": "/my-exams/", "tone": "danger"},
            {"label": "已考", "value": taken, "unit": "场",
             "icon": "bi-check2-circle", "url": "/my-exams/", "tone": "success"},
            {"label": "我的班级", "value": classrooms.count(), "unit": "个",
             "icon": "bi-people", "url": "/classrooms/", "tone": "secondary"},
        ]

        todos = []
        if in_progress:
            todos.append({
                "badge": "继续答题", "tone": "danger",
                "title": in_progress.exam.title,
                "desc": "你有一次作答尚未交卷，计时仍在继续",
                "url": f"/exams/{in_progress.exam_id}/take/",
            })
        if ongoing and (not in_progress or ongoing.id != in_progress.exam_id):
            todos.append({
                "badge": "可以作答", "tone": "warning",
                "title": ongoing.title,
                "desc": f"截止 {ongoing.end_time:%m-%d %H:%M}" if ongoing.end_time else "进行中",
                "url": f"/exams/{ongoing.id}/take/",
            })
        if upcoming:
            todos.append({
                "badge": "待开始", "tone": "info",
                "title": upcoming.title,
                "desc": f"开始 {upcoming.start_time:%m-%d %H:%M}" if upcoming.start_time else "待定",
                "url": "/my-exams/",
            })

        latest_payload = None
        if latest:
            total = latest.exam.paper.total_score_float or 0
            latest_payload = {
                "title": latest.exam.title,
                "score": float(latest.score or 0),
                "total": total,
                "passed": bool(total and float(latest.score or 0) >= float(latest.exam.effective_pass_score or 0)),
                "graded": latest.status == AttemptStatus.GRADED,
                "url": f"/exams/{latest.exam_id}/result/",
            }

        return {
            "role": "student",
            "stats": stats,
            "todos": todos[:4],
            "latest": latest_payload,
            "classrooms": list(classrooms.values("id", "name")[:3]),
            "actions": [
                {"title": "每日练习", "url": "/practice/", "icon": "bi-lightning-charge"},
                {"title": "我的错题本", "url": "/my-wrong-book/", "icon": "bi-journal-bookmark"},
            ],
        }
