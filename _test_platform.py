# -*- coding: utf-8 -*-
"""跨平台打开文件 + 输出文件名归属测试。

覆盖两个 bug 的修复（均为 macOS 实机暴露）：

  A. `App._open` 按平台分支选打开方式：
       Windows → os.startfile / macOS → open / Linux → xdg-open
     旧写法用 AttributeError 探路（os.startfile 不存在就换 xdg-open），
     macOS 上会掉进 Linux 分支跑 xdg-open —— macOS 没这个命令，
     FileNotFoundError 直接冒到 tk 回调，表现"点了打开文件没反应"。
     另：任何打开失败都要弹框告知，不能静默吞掉。

  B. 输出文件名归属（`_name_auto`）：
     预设名 / 用户手选的文件名**不许**被 `_sync_sel → _auto_name`
     冲成 `SAT_20261026.xlsx`。只有"文件名本身就是自动生成的"时才跟着
     选择变。触发路径：① 设置窗保存后 _sync_sel；② 手动点表格行切换勾选。

用法：python _test_platform.py
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tkinter as tk                                     # noqa: E402
import promax_plan_gui as gui                             # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OK = 0
BAD = 0


def check(name, cond, detail=""):
    global OK, BAD
    if cond:
        OK += 1
        print("  [OK]   %s" % name)
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
    # 非空：presets() 见到空列表会 save_config 写真实 config.json
    "presets": [
        {"name": "混合演示_DEMO", "files": ["DEMO-Digital", "DEMO-C-Band"],
         "filename": "混合演示_DEMO_{date8}", "pause": True},
        {"name": "卫星Ku波段_DEMO", "files": ["DEMO-Ku-Band"],
         "filename": "卫星Ku波段_DEMO_{date8}", "pause": True},
    ],
}
gui.load_config = lambda *a, **k: dict(DEMO)


class Dialogs(object):
    """把 messagebox 换成记录器（模态框会卡住测试）"""

    def __init__(self):
        self.calls = []
        self.answer = True

    def askyesno(self, title, msg="", **k):
        self.calls.append(("askyesno", title, msg))
        return self.answer

    def showinfo(self, title, msg="", **k):
        self.calls.append(("showinfo", title, msg))

    def showwarning(self, title, msg="", **k):
        self.calls.append(("showwarning", title, msg))

    def showerror(self, title, msg="", **k):
        self.calls.append(("showerror", title, msg))

    def last(self, kind):
        for c in reversed(self.calls):
            if c[0] == kind:
                return c
        return None


DLG = Dialogs()
gui.messagebox.askyesno = DLG.askyesno
gui.messagebox.showinfo = DLG.showinfo
gui.messagebox.showwarning = DLG.showwarning
gui.messagebox.showerror = DLG.showerror


def demo_only_dir():
    """只扫「复制了 DEMO-* 表」的隔离目录（同 _test_retest.py 的做法）"""
    tmp = tempfile.mkdtemp(prefix="pmcontorl_plat_")
    for band in ("TER", "SAT"):
        src = os.path.join(HERE, "CH", band)
        dst = os.path.join(tmp, band)
        os.makedirs(dst, exist_ok=True)
        if os.path.isdir(src):
            for f in os.listdir(src):
                if f.startswith("DEMO-"):
                    shutil.copy2(os.path.join(src, f), os.path.join(dst, f))
    return tmp


# ================================================================ A. 打开文件
def test_open():
    print("\n--- A) _open 跨平台分支")

    path = os.path.join(HERE, "demo.xlsx")
    real_startfile = getattr(os, "startfile", None)
    real_popen = gui.subprocess.Popen
    real_platform = sys.platform
    real_name = os.name

    opened = {"startfile": [], "popen": []}

    def fake_startfile(p):
        opened["startfile"].append(p)

    def fake_popen(argv, *a, **k):
        opened["popen"].append(list(argv))
        return None

    try:
        os.startfile = fake_startfile
        gui.subprocess.Popen = fake_popen

        # A1 macOS：必须用 open，且不能碰 startfile
        sys.platform = "darwin"
        opened["startfile"], opened["popen"] = [], []
        DLG.calls = []
        gui.App._open(path)
        check("macOS → 调 open", opened["popen"] == [["open", path]],
              str(opened["popen"]))
        check("macOS → 不调 startfile", not opened["startfile"])
        check("macOS → 不弹错误框", DLG.last("showerror") is None)

        # A2 Windows：startfile
        sys.platform = "win32"
        opened["startfile"], opened["popen"] = [], []
        DLG.calls = []
        gui.App._open(path)
        check("Windows → 调 startfile", opened["startfile"] == [path],
              str(opened["startfile"]))
        check("Windows → 不调 Popen", not opened["popen"])

        # A3 Linux：xdg-open（os.name 也要改成 posix，否则在 Windows 上
        #     测试会走 nt 分支）
        sys.platform = "linux"
        os.name = "posix"
        opened["startfile"], opened["popen"] = [], []
        DLG.calls = []
        gui.App._open(path)
        check("Linux → 调 xdg-open", opened["popen"] == [["xdg-open", path]],
              str(opened["popen"]))
        check("Linux → 不调 startfile", not opened["startfile"])

        # A4 打开失败：弹框告知，不能静默/不能抛到 tk 回调
        def boom(argv, *a, **k):
            raise OSError("no such command: %s" % argv[0])
        gui.subprocess.Popen = boom
        sys.platform = "darwin"
        DLG.calls = []
        gui.App._open(path)          # 不抛异常就算过
        err = DLG.last("showerror")
        check("打开失败 → 弹错误框（不静默）", err is not None, str(DLG.calls))
        check("打开失败 → 框里带路径",
              err is not None and path in err[2], str(err))
    finally:
        gui.subprocess.Popen = real_popen
        sys.platform = real_platform
        os.name = real_name
        if real_startfile is None:
            try:
                del os.startfile
            except AttributeError:
                pass
        else:
            os.startfile = real_startfile


# ================================================================ B. 文件名归属
def test_name_owner():
    print("\n--- B) 输出文件名归属（预设名不被冲成 SAT_xxx）")

    demo_dir = demo_only_dir()
    outdir = tempfile.mkdtemp(prefix="pmcontorl_nameout_")
    root = tk.Tk()
    try:
        app = gui.App(root, demo_dir)
        root.update()
        bad = [it.file for it in app.items if not it.file.startswith("DEMO-")]
        if bad:
            raise SystemExit("!! 扫到非演示表，拒绝继续：%s" % bad)

        app.outdir_var.set(outdir)
        ter = [it for it in app.items if it.band == "TER"]
        sat = [it for it in app.items if it.band == "SAT"]
        check("演示表齐了（TER+SAT 都有）", len(ter) >= 1 and len(sat) >= 1,
              "TER=%d SAT=%d" % (len(ter), len(sat)))

        def base():
            return os.path.basename(app.out_var.get())

        # ---- B1 手动勾选的自动名（原有行为，不能丢）----
        app._toggle_all()                       # 全选
        for it in ter:                          # 再退掉射频 → 只剩卫星
            if app.check_vars[it.key].get():
                app._toggle(it.key)
        n1 = base()
        check("手动多选卫星 → 自动名 SAT_日期",
              n1.startswith("SAT_") and n1.endswith(".xlsx"), n1)
        check("自动名标志位已置", app._name_auto is True)

        # ---- B2 预设名 + 设置窗保存（_sync_sel）→ 不被冲 ----
        preset = [p for p in DEMO["presets"] if p["name"] == "混合演示_DEMO"][0]
        app._apply_preset(preset)
        n2 = base()
        d8 = time.strftime("%Y%m%d")
        check("应用预设 → 文件名按分组", n2 == "混合演示_DEMO_%s.xlsx" % d8, n2)
        check("预设名不是自动名", app._name_auto is False)

        app._build_presets()
        app._sync_sel()                         # 设置窗保存后走的就是这条
        check("设置窗保存后 → 预设名还在（bug 修复点①）",
              base() == "混合演示_DEMO_%s.xlsx" % d8, base())

        # ---- B3 预设名 + 手动点行切换勾选 → 不被冲 ----
        k = sat[0].key
        app._toggle(k)                          # 手动切换一行
        check("手动切换勾选后 → 预设名还在（bug 修复点②）",
              base() == "混合演示_DEMO_%s.xlsx" % d8, base())
        app._toggle(k)                          # 切回来

        # ---- B4 用户手输的名字也不被冲 ----
        app.out_var.set(os.path.join(outdir, "我的手输文件.xlsx"))
        app._name_auto = False
        app._sync_sel()
        check("用户手输文件名 → 不被冲",
              base() == "我的手输文件.xlsx", base())

        # ---- B5 自动名仍然跟着选择变（没丢原功能）----
        for it in app.items:                       # 先全不选（明确名会保留）
            if app.check_vars[it.key].get():
                app._toggle(it.key)
        app.out_var.set("")                        # 清掉明确名，之后交给自动
        app._name_auto = False
        for it in sat:                             # 勾两张卫星 → SAT_
            app._toggle(it.key)
        check("多选卫星 → 自动名 SAT_日期", base().startswith("SAT_"), base())
        app._toggle(sat[0].key)                    # 退一张 → 变单张表名
        check("退到单选 → 跟随选择变成表名",
              base() == "%s_%s.xlsx" % (sat[1].name, d8), base())
        app._toggle(sat[0].key)                    # 再勾上 → 回 SAT_
        check("再勾上 → 跟随选择回 SAT_", base().startswith("SAT_"), base())

        # ---- B6 单选 → 表名_日期 ----
        for it in app.items:
            if app.check_vars[it.key].get():
                app._toggle(it.key)
        app._toggle(sat[-1].key)
        check("单选 → 表名_日期",
              base() == "%s_%s.xlsx" % (sat[-1].name, d8), base())

        # ---- B7 取消所有勾选：自动名清空，明确名保留 ----
        app._toggle(sat[-1].key)                # 全不选
        check("自动名 + 全不选 → 清空", app.out_var.get() == "",
              repr(app.out_var.get()))
        app._apply_preset(preset)               # 再来个预设名
        app._toggle_all()                       # 全选
        app._toggle_all()                       # 全不选
        check("明确名 + 全不选 → 不清空",
              base() == "混合演示_DEMO_%s.xlsx" % d8, base())
    finally:
        root.destroy()
        shutil.rmtree(demo_dir, ignore_errors=True)
        shutil.rmtree(outdir, ignore_errors=True)


def main():
    test_open()
    test_name_owner()
    print("\n通过 %d   失败 %d" % (OK, BAD))
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
