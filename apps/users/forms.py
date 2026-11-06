"""
页面用的 Django 表单。

为什么不直接复用 DRF 序列化器？
  页面走的是「表单 POST + 会话」，需要把错误渲染到对应输入框旁边，
  Django Form 的 ``field.errors`` 天然适配模板；DRF 序列化器更适合 JSON 接口。
两者校验规则保持一致（同样调用 Django 的密码强度校验）。
"""

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password

from apps.common.constants import Role

from .constants import grade_year_choices

User = get_user_model()

BS_INPUT = {"class": "form-control form-control-lg"}
BS_SELECT = {"class": "form-select form-select-lg"}
BS_CHECK = {"class": "form-check-input"}


class LoginForm(forms.Form):
    """登录表单：账号支持 用户名 / 邮箱 / 学号。"""

    username = forms.CharField(
        label="账号", max_length=150, widget=forms.TextInput(
            attrs={**BS_INPUT, "placeholder": "用户名 / 邮箱 / 学号", "autofocus": True}
        )
    )
    password = forms.CharField(
        label="密码", widget=forms.PasswordInput(
            attrs={**BS_INPUT, "placeholder": "请输入密码"}
        )
    )
    remember_me = forms.BooleanField(
        label="记住我", required=False, initial=False,
        widget=forms.CheckboxInput(attrs=BS_CHECK),
    )

    def clean_username(self):
        return self.cleaned_data["username"].strip()


class RegisterForm(forms.ModelForm):
    """注册表单。

    学号 / 工号不由用户填写：学生选年级后由系统派发，教师账号注册入口不开放。
    """

    password = forms.CharField(
        label="密码", min_length=6, widget=forms.PasswordInput(
            attrs={**BS_INPUT, "placeholder": "请输入密码"}
        ),
    )
    password2 = forms.CharField(
        label="确认密码", widget=forms.PasswordInput(
            attrs={**BS_INPUT, "placeholder": "请再次输入密码"}
        ),
    )
    grade_year = forms.ChoiceField(
        label="入学年级", widget=forms.Select(attrs=BS_SELECT),
        help_text="系统据此派发学号",
    )

    class Meta:
        model = User
        fields = ["username", "email", "real_name", "role", "grade_year", "phone", "school"]
        widgets = {
            "username": forms.TextInput(attrs={**BS_INPUT, "placeholder": "登录账号，3 位以上"}),
            "email": forms.EmailInput(attrs={**BS_INPUT, "placeholder": "用于找回密码"}),
            "real_name": forms.TextInput(attrs={**BS_INPUT, "placeholder": "真实姓名"}),
            "role": forms.Select(attrs=BS_SELECT),
            "phone": forms.TextInput(attrs={**BS_INPUT, "placeholder": "选填"}),
            "school": forms.TextInput(attrs={**BS_INPUT, "placeholder": "选填，如：计算机学院"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 年级选项随当前年份滚动；学号 = 年级 + 顺位，由系统派发
        self.fields["grade_year"].choices = grade_year_choices()
        # 公开注册只产出学生账号（D-5 / Q-3）：教师与管理员只能由管理端创建。
        # 字段本身保留（接口签名不变），但只留一个选项，并在页面上以固定文案展示。
        self.fields["role"].choices = [(Role.STUDENT, "学生")]
        self.fields["role"].initial = Role.STUDENT
        self.fields["role"].widget = forms.HiddenInput()
        self.fields["email"].required = True
        self.fields["real_name"].required = True

    # ---------------- 校验 ----------------
    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        if len(username) < 3:
            raise forms.ValidationError("用户名长度不能少于 3 位")
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("该用户名已被占用")
        return username

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("该邮箱已被注册")
        return email

    def clean_password(self):
        password = self.cleaned_data["password"]
        validate_password(password)  # 触发 settings.AUTH_PASSWORD_VALIDATORS
        return password

    def clean(self):
        cleaned = super().clean()
        password, password2 = cleaned.get("password"), cleaned.get("password2")
        if password and password2 and password != password2:
            self.add_error("password2", "两次输入的密码不一致")
        return cleaned


class ProfileForm(forms.ModelForm):
    """个人资料编辑表单。

    刻意不含 ``student_no``：学号 / 工号由系统派发，是身份标识，
    用户不可自行修改（页面上以只读方式展示）。
    """

    class Meta:
        model = User
        fields = ["email", "real_name", "phone", "school", "avatar"]
        widgets = {
            "email": forms.EmailInput(attrs=BS_INPUT),
            "real_name": forms.TextInput(attrs=BS_INPUT),
            "phone": forms.TextInput(attrs=BS_INPUT),
            "school": forms.TextInput(attrs=BS_INPUT),
            "avatar": forms.ClearableFileInput(attrs={"class": "form-control"}),
        }

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("该邮箱已被其他账号使用")
        return email


class ChangePasswordForm(forms.Form):
    """页面改密表单。"""

    old_password = forms.CharField(label="原密码", widget=forms.PasswordInput(attrs=BS_INPUT))
    new_password = forms.CharField(
        label="新密码", min_length=6, widget=forms.PasswordInput(attrs=BS_INPUT)
    )
    confirm_password = forms.CharField(label="确认新密码", widget=forms.PasswordInput(attrs=BS_INPUT))

    def clean_new_password(self):
        value = self.cleaned_data["new_password"]
        validate_password(value)
        return value

    def clean(self):
        cleaned = super().clean()
        new, confirm = cleaned.get("new_password"), cleaned.get("confirm_password")
        if new and confirm and new != confirm:
            self.add_error("confirm_password", "两次输入的新密码不一致")
        if new and new == cleaned.get("old_password"):
            self.add_error("new_password", "新密码不能与原密码相同")
        return cleaned
