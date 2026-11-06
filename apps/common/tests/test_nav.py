"""导航按角色分流的测试（AC-01 ~ AC-05 / AC-08 / AC-03 / U-7）。

D-1 的根因是 ``templates/base.html`` 里先判 ``is_teacher``——管理员天然满足
（``User.is_teacher`` 对管理员返回 True），于是管理端菜单永不可达。
这里用「解析一级 nav-link 的 href 集合」的方式把分流口径钉死，
而不是去断言某段 HTML 的拼接顺序（那样改样式就会误报）。
"""

import re

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.common.constants import Role

User = get_user_model()

PASSWORD = "Exam@123456"

# 匹配「一级导航链接」：class 以 nav-link 开头，紧跟的 href 在 80 字符内
NAV_LINK_RE = re.compile(r'class="nav-link[^"]*"[\s\S]{0,80}?href="([^"]+)"')

# 教师业务二级入口（管理员折叠分组里必须有这 9 个）
TEACHER_PATHS = (
    "/question-bank/", "/question-bank/ai-generate/", "/knowledge-points/",
    "/papers/", "/exams/", "/grading/", "/analytics/", "/proctoring/", "/classrooms/",
)
# 管理端一级入口
MANAGE_PATHS = ("/manage/", "/manage/users/", "/manage/org/", "/manage/system/")
# 学生一级入口
STUDENT_PATHS = ("/my-exams/", "/my-wrong-book/", "/analytics/me/", "/practice/", "/classrooms/")


def nav_hrefs(html: str) -> list:
    """取出一级导航里所有 nav-link 的 href。"""
    return NAV_LINK_RE.findall(html)


class NavigationSplitTest(TestCase):
    """三种角色的导航互不污染。"""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            username="nav_admin", email="nav_admin@example.com", password=PASSWORD,
            role=Role.ADMIN, real_name="导航管理员",
        )
        cls.teacher = User.objects.create_user(
            username="nav_teacher", email="nav_teacher@example.com", password=PASSWORD,
            role=Role.TEACHER, real_name="导航教师",
        )
        cls.student = User.objects.create_user(
            username="nav_student", email="nav_student@example.com", password=PASSWORD,
            role=Role.STUDENT, real_name="导航学生",
        )

    def _html(self, user, url: str) -> str:
        client = self.client_class()
        client.force_login(user)
        response = client.get(url)
        self.assertEqual(response.status_code, 200, f"{user.username} {url}")
        return response.content.decode("utf-8")

    # ---------------- AC-01 / AC-02 ----------------
    def test_admin_nav_has_manage_entries_only_as_top_level(self):
        """AC-01：管理员一级菜单只有管理类，不含 9 个教师业务项。"""
        hrefs = nav_hrefs(self._html(self.admin, "/manage/"))
        for path in MANAGE_PATHS:
            self.assertIn(path, hrefs, f"缺少管理入口 {path}")
        for path in TEACHER_PATHS:
            self.assertNotIn(path, hrefs, f"教师业务 {path} 不应出现在一级导航")

    def test_admin_sees_teaching_group_as_dropdown(self):
        """AC-02：9 个教学入口收在「教学管理（代管）」折叠分组里。"""
        html = self._html(self.admin, "/manage/")
        self.assertIn("教学管理（代管）", html)
        # 折叠态：Bootstrap 的 dropdown-menu 不带 show
        self.assertNotIn('class="dropdown-menu shadow show"', html)
        for path in TEACHER_PATHS:
            self.assertIn(f'href="{path}"', html, f"折叠分组缺少 {path}")

    # ---------------- AC-04 / AC-05 ----------------
    def test_teacher_nav_grouped(self):
        """教师导航按领域分组：一级是「首页 + 3 个下拉 + 班级」，9 个教学入口在折叠菜单里；
        且看不到任何管理入口。"""
        html = self._html(self.teacher, "/")
        top = nav_hrefs(html)
        self.assertIn("/classrooms/", top)
        self.assertNotIn("/manage/", top, "教师不应看到管理入口")
        # 9 个教学入口在页面的下拉菜单里仍然可达
        for path in TEACHER_PATHS:
            self.assertIn(f'href="{path}"', html, f"缺少教学入口 {path}")

    def test_student_nav_only_student_entries(self):
        """AC-05：学生只有 5 个学生菜单。"""
        hrefs = nav_hrefs(self._html(self.student, "/"))
        for path in STUDENT_PATHS:
            self.assertIn(path, hrefs)
        for path in MANAGE_PATHS:
            self.assertNotIn(path, hrefs)
        for path in ("/question-bank/", "/papers/", "/exams/", "/grading/", "/proctoring/"):
            self.assertNotIn(path, hrefs, f"学生不应看到教学入口 {path}")

    # ---------------- AC-08：管理员落地 ----------------
    def test_admin_lands_on_manage_console(self):
        """AC-08：管理员访问 / 被重定向到 /manage/；登录成功也落到 /manage/。"""
        client = self.client_class()
        client.force_login(self.admin)
        response = client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/manage/")

        fresh = self.client_class()
        login = fresh.post(
            "/login/", {"username": "nav_admin", "password": PASSWORD}
        )
        self.assertEqual(login.status_code, 302)
        self.assertEqual(login["Location"], "/manage/")

    def test_teacher_and_student_home_unchanged(self):
        """AC-11：教师 / 学生首页仍是工作台，不被重定向。"""
        for user in (self.teacher, self.student):
            client = self.client_class()
            client.force_login(user)
            self.assertEqual(client.get("/").status_code, 200, user.username)

    # ---------------- AC-03：代管提示条 ----------------
    def test_admin_sees_delegation_banner_on_teacher_page(self):
        """AC-03：管理员进入任一教师页，顶部出现代管提示条。"""
        for url in ("/question-bank/", "/papers/", "/exams/", "/analytics/", "/proctoring/"):
            html = self._html(self.admin, url)
            self.assertIn("你正在以管理员身份代管教学功能", html, url)
            # P1-4（操作日志）尚未落地，文案只能承诺「执行」，不能承诺「记录」
            self.assertIn("本次操作将按管理员身份执行", html, url)
            self.assertNotIn("身份记录", html, url)

    def test_teacher_does_not_see_delegation_banner(self):
        """AC-04（视觉）：教师自己看不到提示条。"""
        html = self._html(self.teacher, "/question-bank/")
        self.assertNotIn("以管理员身份代管", html)

    def test_developer_block_removed_from_home(self):
        """首页的开发者区块（原 ``{% if user.is_staff %}``）已删除。"""
        for user in (self.teacher, self.student):
            html = self._html(user, "/")
            self.assertNotIn("系统模块（全部完成）", html, user.username)
            self.assertNotIn('href="/admin/"', html, user.username)
            self.assertIn("个人中心", html, user.username)

    # ---------------- U-7：开发者入口不在一级导航 ----------------
    def test_developer_links_moved_off_top_nav(self):
        """U-7：/api/docs/ 与 /admin/ 不再出现在一级导航（已收进 /manage/system/）。"""
        hrefs = nav_hrefs(self._html(self.admin, "/manage/"))
        self.assertNotIn("/api/docs/", hrefs)
        self.assertNotIn("/admin/", hrefs)

        system_page = self._html(self.admin, "/manage/system/")
        self.assertIn('href="/api/docs/"', system_page)
        self.assertIn('href="/admin/"', system_page)
