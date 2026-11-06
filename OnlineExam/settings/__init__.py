"""
配置包入口。

通过环境变量 ``DJANGO_SETTINGS_MODULE`` 决定加载哪套配置：

    OnlineExam.settings.development   本地开发（默认）
    OnlineExam.settings.production    生产环境

之所以拆成包，是为了让「环境差异」只体现在配置文件里，
业务代码永远不需要写 ``if DEBUG`` 这类分支。
"""
