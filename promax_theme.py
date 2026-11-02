# -*- coding: utf-8 -*-
"""深浅色适配 —— 只为一件事存在：传统 tk 控件在 macOS 深色模式下"白字白底"。

问题（2026-11-01 用户实测反馈）：macOS 开深色模式后，「已选频道表」列表里的
频道表名和背景**都是白的**，只有正在测（琥珀）/ 已测完（浅绿）那两行看得见。

原因：ttk 控件（Button / Entry / Treeview / Progressbar）会跟随系统外观，
而 Listbox / Text 属于**传统 tk 控件**，Tk 不替它们做深色适配 ——
背景还是浅色默认值，前景却被系统外观设成了浅色，于是"白字白底"。

对策：不去猜 Tk 会不会自动适配，直接给这些控件**显式配色**。
  · palette()       —— 取当前外观下的色板
  · text_opts()     —— tk.Text 的构造参数（bg/fg/光标/选区）
  · listbox_opts()  —— tk.Listbox 的构造参数
  · canvas_bg()     —— 自绘控件（Canvas 不支持透明）用来"猜"四周底色
  · hint          —— 次要说明文字的灰色（深色下必须是浅灰，否则看不见）

⚠️ 深色判断**只在 macOS 生效**：Windows 的 ttk 用的是 vista 主题，永远浅色，
若只把 Listbox 涂黑，反而和周围不搭（半黑半白）。Linux 的 Gtk 主题五花八门，
同理不动，保持系统默认。
"""
import subprocess
import sys
import tkinter as tk
from tkinter import ttk

# --------------------------------------------------------------- 色板
# 浅色：与改动前完全一致（Windows 外观不变，测试里的颜色断言也不受影响）
LIGHT = {
    "panel": "#f0f0f0",          # 窗口/面板底色（只在猜不出来时兜底）
    "list_bg": "#ffffff",
    "list_fg": "#000000",
    "select_bg": "#cce8ff",
    "select_fg": "#000000",
    "text_bg": "#ffffff",
    "text_fg": "#000000",
    "active_bg": "#ffd966",      # 琥珀：正在测试
    "done_bg": "#d9f2d9",        # 浅绿：已完成
    "idle_bg": "#ffffff",        # 白：排队中
    "hint": "#777777",           # 次要说明文字
    "danger": "#cc0000",         # 警示文字
    "link": "#0a5bd3",           # 可点击的链接（关于页的项目地址）
    "switch_off": "#c9c9c9",     # 开关轨道：关
    "switch_on": "#34c759",      # 开关轨道：开
    "switch_knob": "#ffffff",    # 滑块
    "switch_edge": "#c0c0c0",    # 滑块描边（浅色底上也要有边界）
}

DARK = {
    "panel": "#2b2b2b",
    "list_bg": "#1f1f1f",
    "list_fg": "#e8e8e8",
    "select_bg": "#2f6fd0",
    "select_fg": "#ffffff",
    "text_bg": "#1f1f1f",
    "text_fg": "#e8e8e8",
    "active_bg": "#6b5619",      # 暗琥珀：底暗但比列表底亮，能一眼看出在测哪张
    "done_bg": "#1e4028",        # 暗绿
    "idle_bg": "#1f1f1f",        # 与列表底同色 → 排队行就是普通行
    "hint": "#a8a8a8",
    "danger": "#ff7a7a",         # 暗底上的红要提亮，纯 #c00 对比度不够
    "link": "#6aa8ff",           # 暗底上的链接要提亮（#0a5bd3 几乎看不见）
    "switch_off": "#5a5a5a",
    "switch_on": "#30d158",
    "switch_knob": "#ffffff",
    "switch_edge": "#3a3a3a",
}

_CACHE = {}


def _mac_dark():
    """macOS 是否深色：`defaults read -g AppleInterfaceStyle` 深色时回 Dark。

    浅色模式下这个键根本不存在（退出码 1、无输出），所以"没读到"就是浅色。
    用 subprocess 而不是 Tk —— Tk 8.6 没有暴露系统外观的接口
    （8.7 的 tk::mac::Appearance 在 8.6 上不存在）。
    """
    try:
        p = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=2.0)
        return p.stdout.decode("utf-8", "replace").strip().lower() == "dark"
    except Exception:
        return False


def dark_mode(refresh=False):
    """当前是否走深色色板（结果缓存，`refresh=True` 强制重算）。"""
    if refresh or "dark" not in _CACHE:
        _CACHE["dark"] = _mac_dark() if sys.platform == "darwin" else False
    return _CACHE["dark"]


def palette():
    """当前色板的**副本**（调用方随便改，不会污染模块状态）。"""
    return dict(DARK if dark_mode() else LIGHT)


def c(key):
    """取单个颜色：`c("list_bg")`"""
    return (DARK if dark_mode() else LIGHT).get(key, "#000000")


# --------------------------------------------------------------- 控件参数
def text_opts():
    """tk.Text 的配色参数（只给颜色，不动 border/highlight，避免改布局）。"""
    p = palette()
    return {"background": p["text_bg"], "foreground": p["text_fg"],
            "insertbackground": p["text_fg"],
            "selectbackground": p["select_bg"],
            "selectforeground": p["select_fg"]}


def listbox_opts():
    """tk.Listbox 的配色参数。

    ⚠️ `selectbackground/selectforeground` 也要给：主界面那个"已选频道表"
    是 selectmode="none" 的展示列表用不到，但编辑器/选择框里的列表要靠它，
    默认的深蓝底 + 黑字在深色模式下同样难认。
    """
    p = palette()
    return {"background": p["list_bg"], "foreground": p["list_fg"],
            "selectbackground": p["select_bg"],
            "selectforeground": p["select_fg"]}


def _valid_color(widget, value):
    """value 能不能当颜色用（systemWindowBackgroundColor 这类系统色名也算）。"""
    try:
        widget.winfo_rgb(value)
        return True
    except Exception:
        return False


def canvas_bg(master):
    """猜一个与四周一致的纯色 —— Canvas 不支持透明背景，只能问 ttk 主题。

    顺序：TFrame → TLabelframe → TNotebook → 色板兜底。
    拿到的是"系统色名"（如 SystemButtonFace）也照用，Tk 认得；
    认不得就退到色板里的 panel 色。
    """
    if master is not None:
        try:
            st = ttk.Style(master)
            for name in ("TFrame", "TLabelframe", "TNotebook"):
                try:
                    v = st.lookup(name, "background")
                except tk.TclError:
                    continue
                if v and _valid_color(master, str(v)):
                    return str(v)
        except Exception:
            pass
    return c("panel")


def _lum(hex_color):
    """粗略亮度 0~255（只用来判断"这两种颜色够不够区分"，不当色彩管理用）。"""
    try:
        r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    except Exception:
        return 0
    return (r * 299 + g * 587 + b * 114) // 1000


def contrast_diff(fg, bg):
    """前景/背景亮度差绝对值。经验值：< 60 就很难看清（自检与测试用）。"""
    return abs(_lum(fg) - _lum(bg))
