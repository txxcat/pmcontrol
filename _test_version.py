# -*- coding: utf-8 -*-
"""版本号 / 设置「关于」页 / 多选选择框 测试。

背景（2026-11-02 用户需求）：
  ① 「同步」里下载、上传频道表要能多选，规则与 Windows 一样（Ctrl 加选、Shift 选段）；
  ② 代码里要有版本号，设置里加一页「关于」，显示版本号、更新日期、GitHub 地址等。

本测试锁住三件事：
  A. `promax_version` 是**唯一来源**：版本号格式、日期可解析、项目地址合法，
     而且主窗口标题真的带上了版本号（不是各写各的）；
  B. 设置窗里确实多了一个「关于」页，且版本号 / 更新日期 / 仓库地址都在页面上；
  C. 多选选择框：`multi=True` 时 Listbox 用 extended 模式（= Windows 资源管理器
     那套规则），`indices` 返回全部选中项；单选模式仍只回一个，老代码不受影响。

🔴 只用 DEMO-* 演示表，不读真实 CH/。

用法：python _test_version.py
"""
import os
import re
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tkinter as tk                                       # noqa: E402
import promax_version as ver                                # noqa: E402

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
    "presets": [{"name": "射频数字_DEMO", "files": ["DEMO-Digital"],
                 "filename": "射频数字_DEMO_{date8}", "pause": False}],
}


def demo_dir():
    """只含 DEMO-* 演示表的临时目录（界面里不会出现真实表名）。"""
    tmp = tempfile.mkdtemp(prefix="pmcontrol_ver_")
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


def texts(w):
    """收集控件上的全部文字（含 textvariable 当前值）。"""
    acc = []
    for c in walk(w):
        for opt in ("text", "textvariable"):
            try:
                v = c.cget(opt)
            except Exception:
                continue
            if not v:
                continue
            if opt == "textvariable":
                try:
                    v = c.getvar(v)
                except Exception:
                    v = ""
            if v:
                acc.append(str(v))
    return acc


# ================================================================ A) 版本模块
def test_version_module():
    print("\n--- A) 版本信息模块（唯一来源）")
    check("版本号是 主.次.修订 三段式",
          bool(re.match(r"^\d+\.\d+\.\d+$", ver.VERSION)), ver.VERSION)
    check("更新日期是 YYYY-MM-DD",
          bool(re.match(r"^\d{4}-\d{2}-\d{2}$", ver.RELEASE_DATE)),
          ver.RELEASE_DATE)
    try:
        import datetime
        datetime.datetime.strptime(ver.RELEASE_DATE, "%Y-%m-%d")
        ok_date = True
    except Exception as e:
        ok_date = False
        print("     ", e)
    check("更新日期是真实日期（不是 2026-13-45 这种）", ok_date)
    check("项目地址是 https 的 github 仓库",
          ver.GITHUB_URL.startswith("https://github.com/")
          and ver.GITHUB_URL.count("/") == 4, ver.GITHUB_URL)
    check("发布页地址从仓库地址派生（不会两处写歪）",
          ver.RELEASES_URL.startswith(ver.GITHUB_URL), ver.RELEASES_URL)
    check("反馈地址从仓库地址派生",
          ver.ISSUES_URL.startswith(ver.GITHUB_URL), ver.ISSUES_URL)
    check("version_text 带 v 前缀", ver.version_text() == "v" + ver.VERSION)
    check("标题里带版本号", ver.VERSION in ver.short_title(), ver.short_title())
    check("release_text 同时有版本与日期",
          ver.VERSION in ver.release_text()
          and ver.RELEASE_DATE in ver.release_text(), ver.release_text())
    env = ver.env_text()
    check("运行环境含 Python 版本", "Python" in env, env)
    check("运行环境标明是源码版还是 exe",
          ("源码版" in env) or ("打包版" in env), env)
    at = ver.about_text([("配置", "x.json")])
    check("about_text 含版本 / 日期 / 地址",
          all(s in at for s in (ver.VERSION, ver.RELEASE_DATE,
                                ver.GITHUB_URL)), at)
    check("about_text 能带上调用方给的附加行", "配置：x.json" in at, at)


# ================================================================ B) 关于页
def test_about_tab():
    print("\n--- B) 设置窗「关于」页")
    import promax_plan_gui as gui
    gui.load_config = lambda *a, **k: dict(DEMO)
    gui.App._open = staticmethod(lambda p: None)      # 别真去开浏览器
    src = demo_dir()
    root = tk.Tk()
    root.withdraw()
    try:
        app = gui.App(root, src)
        for _ in range(3):
            root.update_idletasks()
            root.update()
        check("主窗口标题带版本号",
              ver.VERSION in root.title(), root.title())

        app._open_settings()
        for _ in range(5):
            root.update_idletasks()
            root.update()
        win = [w for w in root.winfo_children()
               if isinstance(w, tk.Toplevel) and w.winfo_exists()]
        check("设置窗开出来了", bool(win), win)
        if not win:
            return
        win = win[-1]
        nbs = [w for w in walk(win) if w.winfo_class() == "TNotebook"]
        check("设置窗里有标签页容器", bool(nbs))
        if not nbs:
            return
        nb = nbs[0]
        tabs = [nb.tab(t, "text") for t in nb.tabs()]
        check("多了一页「关于」", "关于" in tabs, tabs)
        check("原有页还在（基本 / 日志 / 原始 JSON）",
              all(t in tabs for t in ("基本", "日志", "原始 JSON")), tabs)
        check("窗口标题也带版本号", ver.VERSION in win.title(), win.title())

        about = texts(nb)               # 关于页的文字（各页都在容器里）
        joined = "\n".join(about)
        check("关于页显示版本号", ver.VERSION in joined)
        check("关于页显示更新日期", ver.RELEASE_DATE in joined)
        check("关于页显示 GitHub 项目地址", ver.GITHUB_URL in joined)
        check("关于页显示运行环境", "Python" in joined)
        check("关于页给出配置文件位置", "config.json" in joined)
        check("关于页有「复制版本信息」按钮",
              any(t == "复制版本信息" for t in about), about[:12])

        # 复制按钮真的能往剪贴板塞东西（报 bug 时一键贴版本）
        btn = [w for w in walk(win)
               if w.winfo_class() == "TButton"
               and str(w.cget("text")) == "复制版本信息"]
        if btn:
            # ⚠️ showinfo 是**模态**的：不桩掉就会一直等人点确定（实测让本套件
            #    从 6 秒涨到 235 秒）。这里只关心剪贴板内容，弹框记下来就行。
            saved_info = gui.messagebox.showinfo
            gui.messagebox.showinfo = lambda *a, **k: None
            try:
                root.clipboard_clear()
                btn[0].invoke()
                root.update()
                got = root.clipboard_get()
            finally:
                gui.messagebox.showinfo = saved_info
            check("复制出来的内容含版本号", ver.VERSION in got, got[:80])
        else:
            check("找到复制按钮", False)

        win.destroy()
    finally:
        try:
            root.destroy()
        except Exception:
            pass
        shutil.rmtree(src, ignore_errors=True)


# ================================================================ C) 多选框
def _drive_dialog(root, title, fn):
    """对话框是 wait_window 阻塞的 → 用 after 在主循环里操作它。

    fn(win, lb) 里做选择 / 点确定。找不到控件就什么都不做（测试会报"没选中"）。
    """
    def go():
        win = None
        for w in root.winfo_children():
            if isinstance(w, tk.Toplevel):
                try:
                    if w.title() == title:
                        win = w
                except Exception:
                    pass
        if win is None:
            root.after(80, go)
            return
        lbs = [w for w in walk(win) if w.winfo_class() == "Listbox"]
        if not lbs:
            return
        fn(win, lbs[0])
    root.after(120, go)


def _click_ok(win, text="确定"):
    for w in walk(win):
        try:
            if w.winfo_class() == "TButton" and str(w.cget("text")) == text:
                w.invoke()
                return
        except Exception:
            pass


def test_pick_dialog():
    print("\n--- C) 多选选择框（Windows 规则）")
    import promax_plan_editor_gui as EG
    items = ["射频 · A", "射频 · B", "卫星 · C", "卫星 · D"]
    root = tk.Tk()
    # ⚠️ 不能 withdraw：Tk 的**键盘**事件只送给焦点控件，而隐藏窗口拿不到焦点
    #    （Ctrl+A 那条用例会恒失败）。对话框本身也是可见的，这里保持真窗口。
    root.geometry("300x200+40+40")
    root.deiconify()
    root.update()
    try:
        # ---- 单选：行为不变，只回一个 index ----
        d = {}

        def pick_one(win, lb):
            lb.selection_clear(0, "end")
            lb.selection_set(2)
            d["mode"] = lb.cget("selectmode")
            _click_ok(win)

        _drive_dialog(root, "单选", pick_one)
        dlg = EG._PickDialog(root, "单选", items)
        check("单选模式用 browse（单击就换选）",
              d.get("mode") == "browse", d.get("mode"))
        check("单选：index = 选中的那项", dlg.index == 2, dlg.index)
        check("单选：indices 也只一项", dlg.indices == [2], dlg.indices)

        # ---- 多选：extended 模式，indices 拿到全部 ----
        d.clear()

        def pick_many(win, lb):
            lb.selection_clear(0, "end")
            for i in (0, 2):
                lb.selection_set(i)       # 等价于 Ctrl + 点击逐个加选
            d["mode"] = lb.cget("selectmode")
            d["count"] = str([w for w in walk(win)] and "")
            d["hint"] = " ".join(texts(win))
            _click_ok(win)

        _drive_dialog(root, "多选", pick_many)
        dlg = EG._PickDialog(root, "多选", items, multi=True,
                             hint="Ctrl + 点击 加选")
        check("多选模式用 extended（= Windows 资源管理器规则）",
              d.get("mode") == "extended", d.get("mode"))
        check("多选：indices 拿到两项", dlg.indices == [0, 2], dlg.indices)
        check("多选：index 仍是第一项（老调用方不受影响）",
              dlg.index == 0, dlg.index)
        check("多选框里写明了 Ctrl / Shift 规则",
              "Ctrl" in d.get("hint", "") or "Ctrl" in d.get("hint", ""),
              d.get("hint")[:60])

        # ---- 多选 + Ctrl+A 全选 ----
        d.clear()

        def pick_all(win, lb):
            # Tk 把**键盘**事件送给焦点控件（不是事件目标），所以先给焦点
            lb.focus_force()
            win.update()
            lb.event_generate("<Control-a>")
            d["after_ctrl_a"] = len(lb.curselection())
            _click_ok(win)

        _drive_dialog(root, "全选", pick_all)
        dlg = EG._PickDialog(root, "全选", items, multi=True)
        check("Ctrl+A 全选", d.get("after_ctrl_a") == len(items),
              d.get("after_ctrl_a"))
        check("全选后 indices = 全部", dlg.indices == [0, 1, 2, 3], dlg.indices)

        # ---- 预选（上传对话框默认勾上当前打开的表）----
        d.clear()

        def keep_preset(win, lb):
            d["sel"] = list(lb.curselection())
            _click_ok(win)

        _drive_dialog(root, "预选", keep_preset)
        dlg = EG._PickDialog(root, "预选", items, multi=True, preselect=[1, 2])
        check("预选项被提前勾上", d.get("sel") == [1, 2], d.get("sel"))
        check("预选 + 直接确定 = 就传这两项",
              dlg.indices == [1, 2], dlg.indices)

        # ---- 取消：什么都不选，调用方应看到空 ----
        _drive_dialog(root, "取消", lambda w, lb: _click_ok(w, "取消"))
        dlg = EG._PickDialog(root, "取消", items, multi=True)
        check("取消后 indices 为空（调用方直接 return，不会误传）",
              dlg.indices == [] and dlg.index is None, dlg.indices)
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def main():
    t0 = time.time()
    test_version_module()
    test_about_tab()
    test_pick_dialog()
    print("\n通过 %d / 失败 %d   （%.1f 秒）" % (OK, BAD, time.time() - t0))
    return 0 if BAD == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
