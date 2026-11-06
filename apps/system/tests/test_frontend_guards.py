"""前端静态守卫：钉住几类「改回去就静默失效」的写法。

这些不是业务测试，而是对模板做静态断言——因为它们的故障模式是
**页面看起来完全正常、功能其实没生效**：

* BUG-03：单个知识点勾选不触发题池刷新。复选框能点、勾上也有蓝底，
  但 `dataset.kp` 是空字符串（无值属性），按真值判断就永远 return。
  接口测试看不到（请求根本没发出去），人工点一遍也容易以为是"题池本来就这么多"。
* BUG-02：编辑器把空选项行也提交。`validate()` 只看有内容的、`collect()` 全交，
  两套口径，校验能过、提交被拒。

这两类问题的共同点是「写法一改就复发，且复发后不会报错」，所以用静态断言守。
"""

import io

from django.conf import settings
from django.test import SimpleTestCase


def _read(relative_path: str) -> str:
    return io.open(settings.BASE_DIR / relative_path, encoding="utf-8").read()


class ComposeKpCheckboxGuardTest(SimpleTestCase):
    """BUG-03：知识点复选框的监听器必须判「属性存在」，不能判「属性值」。"""

    def setUp(self):
        self.source = _read("templates/papers/compose.html")

    def test_checkbox_uses_valueless_data_attribute(self):
        """模板里的复选框用的是无值属性 `data-kp`——这是前提，改了就说明修复方式要复核。"""
        self.assertIn("data-kp>", self.source, "知识点复选框应保持无值属性 data-kp")

    def test_listener_checks_attribute_presence(self):
        """监听器用 hasAttribute：这才是「这是不是一个知识点复选框」的正确判据。"""
        self.assertIn(
            'hasAttribute("data-kp")', self.source,
            "kpTree 的 change 监听器要用 hasAttribute('data-kp') 判断，"
            "否则单个勾选会被静默拦掉（BUG-03）",
        )

    def test_listener_does_not_judge_by_truthiness(self):
        """反向断言：不许退回「按 dataset.kp 的真值判断」的写法。"""
        self.assertNotIn(
            "!event.target.dataset.kp", self.source,
            "不能按 dataset.kp 的真值判断——无值属性的值是空字符串（falsy），"
            "会让单个勾选永远 return（BUG-03 原样复发）",
        )

    def test_bulk_toggle_still_selects_by_attribute(self):
        """「全选/清空」走的是属性选择器，修复不能把它弄坏。"""
        self.assertIn('querySelectorAll("#kpTree input[data-kp]")', self.source)


class QuestionEditorEmptyOptionGuardTest(SimpleTestCase):
    """BUG-02：编辑器提交前必须过滤空选项行，且后端不放宽 allow_blank。"""

    def setUp(self):
        self.source = _read("templates/questions/editor.html")

    def test_collect_filters_blank_options(self):
        self.assertIn(
            "filter(o => o.content)", self.source,
            "collect() 必须过滤空选项行，否则默认 4 个空行会被原样提交（BUG-02）",
        )

    def test_validate_and_collect_share_the_same_criteria(self):
        """校验和提交都要「只看有内容的」——两套口径正是 BUG-02 的成因。"""
        self.assertGreaterEqual(
            self.source.count("o.content"), 2,
            "validate() 和 collect() 都应基于「content 非空」判定",
        )

    def test_backend_still_rejects_blank_option_content(self):
        """后端不能为了"修缺陷"放宽成 `content` 允许为空。

        注意不能一刀切断言整个文件没有 `allow_blank=True`——`label`（选项字母）
        和 `reference_answer`（主观题答案）本来就允许空，只有 `content` 不行。
        这里精确锁住 `content` 的写法：不带参数即默认 `allow_blank=False`。
        """
        serializers_source = _read("apps/questions/serializers.py")
        self.assertIn(
            "content = serializers.CharField()", serializers_source,
            "选项内容必须保持 allow_blank=False（不传参即默认），"
            "放宽会让空行静默入库成脏数据（BUG-02 的错误修法）",
        )


class MonitorStatusBadgeGuardTest(SimpleTestCase):
    """BUG-04：监控页状态徽章读 `computed_status_display`，兜底不能是空串。"""

    def setUp(self):
        self.source = _read("templates/exam/monitor.html")

    def test_badge_reads_display_field(self):
        self.assertIn("ov.computed_status_display", self.source)

    def test_fallback_is_a_visible_placeholder(self):
        """兜底用「—」而不是 `''`：字段缺失时宁可显示"未知"，也不要空一块。"""
        self.assertIn(
            "computed_status_display || '—'", self.source,
            "徽章兜底不能是空字符串，否则文案缺失时整块空白（BUG-04）",
        )
