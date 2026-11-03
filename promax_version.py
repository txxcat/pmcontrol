# -*- coding: utf-8 -*-
"""程序版本信息 —— **唯一来源**。

发版时只改这个文件的 `VERSION` / `RELEASE_DATE`，其它地方（主窗口标题、
设置→关于页、以后可能的自动更新检查）统统从这里取，不再各自写死字符串。

⚠️ 别和 `promax_plan_write.VERSION` 搞混：那个是**Promax 文件格式版本**
（写进 XML 的 `VERSION="1.3"`，由仪器侧约定），与程序自身的版本号无关。

版本号规则（语义化版本）：
    主版本.次版本.修订号
    · 修订号 +1：只修 bug，界面与行为不变（如 1.1.0 → 1.1.1）
    · 次版本 +1：加功能、保持兼容（如 1.1.0 → 1.2.0）
    · 主版本 +1：不兼容的大改（配置文件格式换了、命令流程改了）
"""
import platform
import sys

APP_NAME = "PROMAX 频道表批量测试"     # 界面标题用的名字
APP_ID = "pmcontrol"                    # 仓库 / 可执行文件名（小写无空格）
VERSION = "1.3.0"
RELEASE_DATE = "2026-11-03"             # 这一版定稿的日期（不是运行时时间）

GITHUB_URL = "https://github.com/txxcat/pmcontrol"
RELEASES_URL = GITHUB_URL + "/releases/latest"
ISSUES_URL = GITHUB_URL + "/issues"
AUTHOR = "txxcat"
LICENSE_NAME = "MIT"


def version_text():
    """`v1.2.0`"""
    return "v" + VERSION


def short_title():
    """窗口标题前缀：`PROMAX 频道表批量测试 v1.2.0`"""
    return "%s %s" % (APP_NAME, version_text())


def release_text():
    """`v1.2.0（2026-11-02）`"""
    return "%s（%s）" % (version_text(), RELEASE_DATE)


def env_text():
    """运行环境一行 —— 报 bug 时一起贴，省一轮来回问。

    单独成函数（不碰 Tk）是为了能在没有图形环境的测试里直接断言。
    """
    bits = ["Python %s" % sys.version.split()[0]]
    try:                                # Tk 可能没装（服务器 / 精简 Python）
        import tkinter
        bits.append("Tk %s" % tkinter.TkVersion)
    except Exception:
        bits.append("Tk —")
    bits.append("%s %s" % (platform.system(), platform.release()))
    bits.append("打包版（exe）" if getattr(sys, "frozen", False) else "源码版")
    return " · ".join(bits)


def about_text(extra=None):
    """「关于」页要展示的纯文本（复制到剪贴板用，也便于测试断言）。"""
    lines = ["%s %s" % (APP_NAME, release_text()),
             "运行环境：%s" % env_text(),
             "项目地址：%s" % GITHUB_URL,
             "问题反馈：%s" % ISSUES_URL,
             "%s License · © 2026 %s" % (LICENSE_NAME, AUTHOR)]
    for k, v in (extra or []):
        lines.append("%s：%s" % (k, v))
    return "\n".join(lines)
