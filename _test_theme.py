# -*- coding: utf-8 -*-
"""深色模式配色 + 设置窗滑动开关测试。

背景（2026-11-01 用户 macOS 实测反馈）：
  ① macOS 开深色模式后，「已选频道表」里的频道表名和背景**都是白的**，
     只有正在测（琥珀）/ 已测完（浅绿）那几行看得见。
     原因：ttk 控件跟随系统外观，Listbox / Text 这类传统 tk 控件不跟随 ——
     底还是浅色默认，字却被系统设成浅色。
  ② 设置窗里几个 yes/no 下拉框要改成滑动开关。

本测试锁住三件事：
  A. 两套色板的字/底对比度都够（不是"看着还行"，用亮度差断言）；
  B. 深色判断只在 macOS 生效（Windows 的 ttk 永远浅色，只涂黑列表反而更丑）；
  C. 开关是"点一下→变量翻转→画面跟上"的单向数据流，且值真的落到配置 json。
     顺带守住"设置窗里不再有第二个 yes/no 下拉框"。

用法：python _test_theme.py
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tkinter as tk                                       # noqa: E402
import promax_plan_gui as gui                               # noqa: E402
import promax_theme as theme                                # noqa: E402
from promax_widgets import ToggleSwitch, _mix               # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OK = 0
BAD = 0


def check(name, cond, detail=""):
    global OK, BAD
    if cond:
        OK += 1
        print("  [ok] %s" % name)
    else:
        BAD += 1
        print("  [FAIL] %s   %s" % (name, detail))


DEMO = {
    "window": {"width": 1240, "height": 780},
    "ip_history": ["127.0.0.1"], "last_ip": "127.0.0.1", "port": 2222,
    "out_dir": "", "out_dir_history": [], "settle": 0.15,
    "settle_ter": 0.3, "settle_sat": 3.0, "retry_ter": 3, "retry_sat": 5,
    "retry_step_ter": 0.3, "retry_step_sat": 6.0,
    "pause_between": True, "auto_reconnect": True, "reconnect_interval": 10.0,
    "export_format": "xlsx", "log_dir": "", "log_name": "promax_test.log",
    "log_timestamps": True, "log_timing": True, "log_cmd_timing": False,
    "log_separate": False,
    "presets": [
        {"name": "射频数字_DEMO", "files": ["DEMO-Digital"],
         "filename": "射频数字_DEMO_{date8}", "pause": False},
        {"name": "混合演示_DEMO", "files": ["DEMO-Digital", "DEMO-C-Band"],
         "filename": "混合演示_DEMO_{date8}", "pause": True},
    ],
}
# 不碰磁盘上的真配置（那里面有真实仪器 IP 与输出目录历史）
gui.load_config = lambda *a, **k: dict(DEMO)


def demo_dir():
    """只含 DEMO-* 演示表的临时目录，避免把本地真实频道表名列进界面。"""
    tmp = tempfile.mkdtemp(prefix="pmcontrol_theme_")
    for band in ("TER", "SAT"):
        src = os.path.join(HERE, "CH", band)
        dst = os.path.join(tmp, band)
        os.makedirs(dst, exist_ok=True)
        if os.path.isdir(src):
            for f in os.listdir(src):
                if f.startswith("DEMO-"):
                    shutil.copy2(os.path.join(src, f), os.path.join(dst, f))
    return tmp


def walk(w, out=None):
    out = [] if out is None else out
    for ch in w.winfo_children():
        out.append(ch)
        walk(ch, out)
    return out


def pump(root, until, timeout=3.0):
    """转事件循环直到 until() 成立（滑动动画要真的跑几帧）。"""
    t0 = time.time()
    while not until() and time.time() - t0 < timeout:
        root.update()
        time.sleep(0.02)


def test_palettes():
    print("\n--- A) 两套色板：字和底必须分得开（亮度差 ≥ 60）")
    for tag, pal in (("浅色", theme.LIGHT), ("深色", theme.DARK)):
        for what, fg, bg in (
                ("列表字/列表底", pal["list_fg"], pal["list_bg"]),
                ("正在测(琥珀)行", pal["list_fg"], pal["active_bg"]),
                ("已完成(绿)行", pal["list_fg"], pal["done_bg"]),
                ("日志正文/日志底", pal["text_fg"], pal["text_bg"]),
                ("提示文字/列表底", pal["hint"], pal["list_bg"]),
                ("警示文字/列表底", pal["danger"], pal["list_bg"]),
                ("链接文字/列表底", pal["link"], pal["list_bg"])):
            d = theme.contrast_diff(fg, bg)
            check("%s：%s 对比 %d" % (tag, what, d), d >= 60,
                  "%s / %s 亮度差只有 %d —— 看不见" % (fg, bg, d))
        check("%s：排队行与列表底同色（不突兀）" % tag,
              pal["idle_bg"] == pal["list_bg"], pal["idle_bg"])
        check("%s：开关关/开两态颜色不同" % tag,
              pal["switch_off"] != pal["switch_on"], pal["switch_off"])
    for k in ("list_bg", "list_fg", "text_bg", "text_fg", "active_bg",
              "done_bg", "idle_bg", "hint", "danger", "switch_off",
              "switch_on", "switch_knob", "switch_edge", "select_bg",
              "select_fg", "panel", "link"):
        check("两套色板都有 %s" % k, k in theme.LIGHT and k in theme.DARK)
    check("色板返回副本（改它不影响模块状态）",
          theme.palette() is not theme.LIGHT
          and theme.palette() is not theme.DARK)


def test_detect():
    print("\n--- B) 深色判断只在 macOS 生效")
    if sys.platform == "darwin":
        check("macOS：dark_mode() 返回 bool",
              isinstance(theme.dark_mode(refresh=True), bool))
    else:
        check("非 macOS 一律浅色板（ttk 不跟随深色，涂黑反而更丑）",
              theme.dark_mode(refresh=True) is False, sys.platform)

    # 造一个"macOS 深色"的全局状态：直接压缓存，验证下游都跟着换
    saved = dict(theme._CACHE)
    try:
        theme._CACHE["dark"] = True
        lo = theme.listbox_opts()
        check("深色时 Listbox 给深底浅字",
              lo["background"] == theme.DARK["list_bg"]
              and lo["foreground"] == theme.DARK["list_fg"], str(lo))
        to = theme.text_opts()
        check("深色时 Text 光标色跟前景（深底上不能是黑光标）",
              to["insertbackground"] == theme.DARK["text_fg"], str(to))
        check("深色时提示文字是浅灰",
              theme.c("hint") == theme.DARK["hint"], theme.c("hint"))
    finally:
        theme._CACHE.clear()
        theme._CACHE.update(saved)


def test_switch(root):
    print("\n--- C) 滑动开关：点一下 → 变量翻转 → 画面跟上")
    var = tk.BooleanVar(value=False)
    hits = []
    sw = ToggleSwitch(root, variable=var, command=lambda v: hits.append(v))
    sw.pack()          # 必须真的布局出来：没映射的控件拿不到焦点，键盘用例会假红
    root.update_idletasks()
    check("初始关：滑块在最左", sw._pos == 0.0, str(sw._pos))
    check("get() 与变量一致", sw.get() is False)

    sw._on_click()
    check("点一下 → 变量变 True", var.get() is True)
    check("command 收到新值", hits == [True], str(hits))
    pump(root, lambda: sw._pos >= 1.0)
    check("滑块滑到最右（不只是变量变了）", sw._pos == 1.0, str(sw._pos))

    hits.clear()
    var.set(False)
    pump(root, lambda: sw._pos <= 0.0)
    check("代码里 var.set(False) 也会带动开关滑回去", sw._pos == 0.0,
          str(sw._pos))
    check("程序改值不触发 command（避免递归）", hits == [], str(hits))

    # 键盘：Tk 把按键事件送给"焦点窗口"，所以必须先给焦点
    #（合成事件 event_generate 也一样，否则绑定看起来"没生效"）
    sw.focus_force()
    root.update()
    sw.event_generate("<space>")
    root.update()
    check("空格键也能切换（键盘可达）", var.get() is True, str(var.get()))
    pump(root, lambda: sw._pos >= 1.0)

    sw.set(False, animate=False)
    check("set(animate=False) 立即到位", sw._pos == 0.0 and sw.get() is False,
          "%s / %s" % (sw._pos, sw.get()))

    check("_mix 中点介于两色之间",
          _mix("#000000", "#ffffff", 0.5) == "#7f7f7f",
          _mix("#000000", "#ffffff", 0.5))
    check("_mix 端点不越界", _mix("#000000", "#ffffff", -3) == "#000000"
          and _mix("#000000", "#ffffff", 5) == "#ffffff")

    # 销毁后再动变量不能炸（动画回调 / trace 都要清干净）
    sw.destroy()
    root.update()
    try:
        var.set(True)
        root.update()
        check("销毁后改变量不报错（动画与 trace 已清）", True)
    except tk.TclError as e:
        check("销毁后改变量不报错（动画与 trace 已清）", False, str(e))


def test_settings_window():
    print("\n--- D) 设置窗：5 个 yes/no 下拉框已换成开关，值真的落盘")
    demo = demo_dir()
    root = tk.Tk()
    saved = {}
    real_save = gui.save_config
    real_info = gui.messagebox.showinfo
    gui.save_config = lambda cfg, path=None: saved.update(cfg)
    gui.messagebox.showinfo = lambda *a, **k: None
    try:
        app = gui.App(root, demo)
        root.update()
        app._open_settings()
        root.update()
        win = [w for w in root.winfo_children()
               if isinstance(w, tk.Toplevel)][-1]
        widgets = walk(win)
        sws = [w for w in widgets if isinstance(w, ToggleSwitch)]
        check("设置窗里有 5 个开关", len(sws) == 5, str(len(sws)))
        if len(sws) == 5:
            got = [s.get() for s in sws]
            check("默认状态取自配置（暂停/重连/时戳/耗时=开，命令级=关）",
                  got == [True, True, True, True, False], str(got))
        combos = [w for w in widgets if w.winfo_class() == "TCombobox"]
        vals = [c.get() for c in combos]
        check("只剩「导出格式」一个下拉框（不再有 yes/no）",
              len(combos) == 1 and vals == ["xlsx"], str(vals))

        if len(sws) == 5:
            sws[0]._on_click()          # 表间暂停：开 → 关
            btn = [b for b in widgets
                   if b.winfo_class() == "TButton"
                   and str(b.cget("text")) == "保存"]
            check("找得到保存按钮", len(btn) == 1, str(len(btn)))
            if btn:
                btn[0].invoke()
                root.update()
                check("保存后 pause_between 变成 False（开关值真的写进配置）",
                      saved.get("pause_between") is False,
                      str(saved.get("pause_between")))
                check("其它布尔项没被牵连",
                      saved.get("auto_reconnect") is True
                      and saved.get("log_timestamps") is True
                      and saved.get("log_cmd_timing") is False,
                      "%s / %s / %s" % (saved.get("auto_reconnect"),
                                        saved.get("log_timestamps"),
                                        saved.get("log_cmd_timing")))
    finally:
        gui.save_config = real_save
        gui.messagebox.showinfo = real_info
        root.destroy()
        shutil.rmtree(demo, ignore_errors=True)


def main():
    test_palettes()
    test_detect()
    root = tk.Tk()
    root.geometry("200x90+40+40")      # 不能 withdraw：没有映射就没有焦点，键盘用例测不了
    try:
        test_switch(root)
    finally:
        root.destroy()
    test_settings_window()
    print("\n通过 %d   失败 %d" % (OK, BAD))
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
