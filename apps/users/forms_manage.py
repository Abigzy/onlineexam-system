"""管理端「用户治理」页面用的 Django 表单。

为什么复用 ModelForm 而不是 DRF 序列化器：
  * 页面走「表单 POST + 会话」，错误要能渲染到对应输入框下方，
    Django Form 的 ``field.errors`` 天然适配模板（AC-14 的字段级回填）；
  * 校验规则与接口侧**刻意保持一致**：都调 ``validate_password``（唯一的密码策略口径）。

与接口的分工：
  * 建号 / 改资料走本模块的表单（服务端渲染，字段级报错）；
  * 重置密码 / 启用停用走既有 API（账号治理的两个原子动作，逻辑只在服务层实现一次）。
"""

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password

from apps.common.constants import Role
from apps.common.exceptions import BusinessError

from .constants import grade_year_choices
from .services import UserService

User = get_user_model()

BS_INPUT = {"class": "form-control"}
BS_SELECT = {"class": "form-select"}


class AdminUserCreateForm(forms.ModelForm):
    """管理员新建账号。

    与公开注册的关键差异：
      * 可以建**教师 / 管理员**（公开注册只能产出学生，D-5）；
      * 初始密码必填，且必须通过 ``AUTH_PASSWORD_VALIDATORS``（AC-15）。
    """

    password = forms.CharField(
        label="初始密码",
        widget=forms.PasswordInput(attrs={**BS_INPUT, "autocomplete": "new-password"}),
        help_text="至少 6 位，不能是纯数字或常见弱口令",
    )
    password2 = forms.CharField(
        label="确认初始密码",
        widget=forms.PasswordInput(attrs={**BS_INPUT, "autocomplete": "new-password"}),
    )
    grade_year = forms.ChoiceField(
        label="入学年级", required=False,   # 学生必填，在 clean() 里按角色卡（教师不需要年级）
        help_text="创建学生时必填：用于派发学号，并校验手填学号与年级一致",
        widget=forms.Select(attrs=BS_SELECT),
    )

    class Meta:
        model = User
        fields = ["username", "email", "real_name", "role",
                  "student_no", "subject", "phone", "school"]
        widgets = {
            "username": forms.TextInput(attrs={**BS_INPUT, "placeholder": "登录账号，3 位以上"}),
            "email": forms.EmailInput(attrs={**BS_INPUT, "placeholder": "用于找回密码"}),
            "real_name": forms.TextInput(attrs={**BS_INPUT, "placeholder": "真实姓名"}),
            "role": forms.Select(attrs=BS_SELECT),
            "student_no": forms.TextInput(attrs={**BS_INPUT, "placeholder": "教师填工号，学生填学号"}),
            # 学科：候选来自库内已有值，但允许自由输入（Q-4 保持自由文本）
            "subject": forms.TextInput(attrs={**BS_INPUT, "list": "subject-options",
                                              "placeholder": "如：数据结构（教师专属）"}),
            "phone": forms.TextInput(attrs={**BS_INPUT, "placeholder": "选填"}),
            "school": forms.TextInput(attrs={**BS_INPUT, "placeholder": "选填，如：计算机学院"}),
        }
        labels = {"student_no": "学号 / 工号", "subject": "任教学科", "role": "角色"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].choices = list(Role.choices)
        self.fields["role"].initial = Role.STUDENT
        self.fields["email"].required = True
        # 年级：学生必填（用于派学号 / 校验手填学号），教师不需要
        self.fields["grade_year"].choices = [("", "请选择年级（学生必填）")] + grade_year_choices()

    # ---------------- 字段级校验 ----------------
    def clean_username(self):
        value = (self.cleaned_data.get("username") or "").strip()
        if len(value) < 3:
            raise forms.ValidationError("用户名长度不能少于 3 位")
        if User.objects.filter(username__iexact=value).exists():
            raise forms.ValidationError("该用户名已被占用")
        return value

    def clean_email(self):
        value = (self.cleaned_data.get("email") or "").strip().lower()
        if User.objects.filter(email__iexact=value).exists():
            raise forms.ValidationError("该邮箱已被注册")
        return value

    def clean_password(self):
        value = self.cleaned_data.get("password") or ""
        validate_password(value)  # 触发 settings.AUTH_PASSWORD_VALIDATORS（AC-15）
        return value

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("password")
        password2 = cleaned.get("password2")
        if password and password2 and password != password2:
            self.add_error("password2", "两次输入的密码不一致")

        student_no = (cleaned.get("student_no") or "").strip()
        if student_no and User.objects.filter(student_no__iexact=student_no).exists():
            self.add_error("student_no", "该学号 / 工号已被使用")

        # 学生必须选年级：学号既靠它派发，也靠它校验手填的学号没有跨年级
        role = cleaned.get("role")
        grade_year = (cleaned.get("grade_year") or "").strip()
        if role == Role.STUDENT:
            if not grade_year:
                self.add_error("grade_year", "创建学生必须选择入学年级")
            elif student_no and not UserService.student_no_matches_grade(student_no, grade_year):
                self.add_error(
                    "student_no",
                    f"学号前 4 位（{student_no[:4]}）与入学年级（{grade_year}）不一致",
                )
        return cleaned

    def save(self, commit: bool = True):
        """落库：密码单独 set_password，账号一律为启用状态（PRD §4.2）。

        学号 / 工号留空时由系统派发——与公开注册、管理端 API 建号是**同一套规则**：
        教师派工号，学生按所选年级派学号（没选年级则按当年），不会落成空编号。
        """
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password"])
        user.is_active = True

        if not (user.student_no or "").strip():
            if user.role == Role.STUDENT:
                # clean() 已保证学生必有年级，这里直接取；不再兜底「当年」，避免悄悄用错年份
                user.student_no = UserService.generate_student_no(
                    self.cleaned_data["grade_year"]
                )
            else:
                user.student_no = UserService.generate_employee_no()

        if commit:
            user.save()
        return user


class AdminUserEditForm(forms.ModelForm):
    """管理员编辑账号。

    * **不含 username**：用户名创建后不可修改（PRD §4.3，页面上按只读文本展示）；
    * **不含 is_active**：状态只能通过行内「停用 / 启用」明确操作，
      避免「改个手机号顺手把人停用了」（PRD §4.3）。
    """

    class Meta:
        model = User
        fields = ["email", "real_name", "role", "student_no", "subject", "phone", "school"]
        widgets = {
            "email": forms.EmailInput(attrs=BS_INPUT),
            "real_name": forms.TextInput(attrs=BS_INPUT),
            "role": forms.Select(attrs=BS_SELECT),
            "student_no": forms.TextInput(attrs=BS_INPUT),
            "subject": forms.TextInput(attrs={**BS_INPUT, "list": "subject-options"}),
            "phone": forms.TextInput(attrs=BS_INPUT),
            "school": forms.TextInput(attrs=BS_INPUT),
        }
        labels = {"student_no": "学号 / 工号", "subject": "任教学科", "role": "角色"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].choices = list(Role.choices)

    def clean_email(self):
        value = (self.cleaned_data.get("email") or "").strip().lower()
        queryset = User.objects.filter(email__iexact=value).exclude(pk=self.instance.pk)
        if queryset.exists():
            raise forms.ValidationError("该邮箱已被其他账号使用")
        return value

    def clean_student_no(self):
        value = (self.cleaned_data.get("student_no") or "").strip()
        if value:
            queryset = User.objects.filter(student_no__iexact=value).exclude(pk=self.instance.pk)
            if queryset.exists():
                raise forms.ValidationError("该学号 / 工号已被使用")
        return value

    def clean_role(self):
        """护栏一：超级管理员角色不可降级（沿用 AdminUserSerializer.validate_role）。"""
        value = self.cleaned_data.get("role")
        if self.instance.is_superuser and value != Role.ADMIN:
            raise forms.ValidationError("超级管理员的角色不可降级")
        return value

    def clean(self):
        """护栏二：不允许把「最后一个启用管理员」降级（AC-18 / U-2）。"""
        cleaned = super().clean()
        role = cleaned.get("role")
        if role and self.instance is not None:
            try:
                UserService.assert_admin_survives(self.instance, role=role)
            except BusinessError as exc:
                self.add_error("role", str(exc.detail))
        return cleaned


__all__ = ["AdminUserCreateForm", "AdminUserEditForm"]
