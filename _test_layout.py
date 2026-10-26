# -*- coding: utf-8 -*-
"""右列（输出文件 / 已选频道表）宽度测试。

背景（macOS 实机暴露）：
  右列原来写死 `ttk.Frame(width=330)` + `pack_propagate(False)`，而 left 是
  **先 pack** 的 —— Tk packer 按 pack 顺序分配，先 pack 的先拿到请求宽度，
  空间不够就压缩最后 pack 的那个。结果窗口一窄，右列先被压扁：
    Windows 1280（Segoe UI 9pt）: left 请求 873，右列拿满 330 ✔
    macOS   1280（Helvetica 13pt）: left 请求 1074，右列只剩 174 ✘

修法（方案②）：
  ① 右列**先 pack**，由可伸缩的左列承担压缩；
  ② 右列不设固定像素宽度、不关几何传播，宽度 = 内容按**当前字体**算出的
     自然宽度（含主题 padding）。字大则列宽自适应，Windows/macOS/Linux 一致。

本测试锁住这几点：不裁剪、不超出可视区、跟随字体变宽、窄窗不被挤扁、pack 顺序。

用法：python _test_layout.py
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tkinter as tk                                      # noqa: E402
import tkinter.font as tkfont                             # noqa: E402
import promax_plan_gui as gui                              # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OK = 0
BAD = 0

# 5 个预设组：预设按钮的每行宽度会撑起左列的最小宽度，
# 数量和名字长度都要接近真实配置，否则测不出"左列把右列挤扁"。
DEMO = {
    "window": {"width": 1280, "height": 780},
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
        {"name": "射频全频道_DEMO", "files": ["DEMO-Digital", "DEMO-Analog"],
         "filename": "射频全频道_DEMO_{date8}", "pause": False},
        {"name": "卫星C波段_DEMO", "files": ["DEMO-C-Band"],
         "filename": "卫星C波段_DEMO_{date8}", "pause": True},
        {"name": "卫星Ku波段_DEMO", "files": ["DEMO-Ku-Band"],
         "filename": "卫星Ku波段_DEMO_{date8}", "pause": True},
        {"name": "混合演示_DEMO", "files": ["DEMO-Digital", "DEMO-C-Band"],
         "filename": "混合演示_DEMO_{date8}", "pause": True},
    ],
}
gui.load_config = lambda *a, **k: dict(DEMO)


def check(name, cond, detail=""):
    global OK, BAD
    if cond:
        OK += 1
        print("  [OK]   %s" % name)
    else:
        BAD += 1
        print("  [FAIL] %s   %s" % (name, detail))


def demo_only_dir():
    """只扫「复制了 DEMO-* 表」的隔离目录"""
    tmp = tempfile.mkdtemp(prefix="pmcontrol_layout_")
    for band in ("TER", "SAT"):
        src = os.path.join(HERE, "CH", band)
        dst = os.path.join(tmp, band)
        os.makedirs(dst, exist_ok=True)
        if os.path.isdir(src):
            for f in os.listdir(src):
                if f.startswith("DEMO-"):
                    shutil.copy2(os.path.join(src, f), os.path.join(dst, f))
    return tmp


def big_fonts(root, size=13):
    """模拟 macOS / 大字号桌面：Tk 默认字体放大到 size pt"""
    for nm in ("TkDefaultFont", "TkTextFont", "TkMenuFont",
               "TkHeadingFont", "TkCaptionFont", "TkSmallCaptionFont"):
        try:
            tkfont.nametofont(nm).configure(size=size)
        except Exception:
            pass
    root.update()


def snap(app, root):
    """量一次当前布局"""
    root.update()
    right = app.right_col
    body = right.master
    left = app.tree.master
    inner = max([w.winfo_reqwidth() for w in right.winfo_children()] or [0])
    return {
        "body": body.winfo_width(),
        "right_w": right.winfo_width(),
        "right_x": right.winfo_x(),
        "right_req": right.winfo_reqwidth(),
        "inner": inner,                     # 右列里最宽的控件
        "left_w": left.winfo_width(),
        "tree_req": app.tree.winfo_reqwidth() + 20,   # 树 + 竖直滚动条
        "visible": min(right.winfo_width(),
                       body.winfo_width() - right.winfo_x()),
    }


def dump(tag, s):
    print("     %-22s 右列 宽%-4d 请求%-4d 内容%-4d 可见%-4d | 左列 宽%-4d"
          % (tag, s["right_w"], s["right_req"], s["inner"], s["visible"],
             s["left_w"]))


def main():
    demo_dir = demo_only_dir()
    root = tk.Tk()
    try:
        app = gui.App(root, demo_dir)
        root.geometry("1280x780")
        root.update()

        bad = [it.file for it in app.items if not it.file.startswith("DEMO-")]
        if bad:
            raise SystemExit("!! 扫到非演示表，拒绝继续：%s" % bad)

        print("\n--- A) 本机字体（Windows 默认 9pt）")
        app0 = snap(app, root)
        dump("默认字体 1280", app0)
        check("右列宽度够装内容（不裁剪）",
              app0["right_w"] >= app0["inner"],
              "宽 %d / 需要 %d" % (app0["right_w"], app0["inner"]))
        check("右列完整可见（没被挤出可视区）",
              app0["right_x"] + app0["right_w"] <= app0["body"],
              "x=%d + %d > %d" % (app0["right_x"], app0["right_w"],
                                  app0["body"]))
        check("左列仍有可用宽度（树能完整显示）",
              app0["left_w"] >= app0["tree_req"],
              "宽 %d / 树需要 %d" % (app0["left_w"], app0["tree_req"]))

        print("\n--- B) 放大字体（模拟 macOS Helvetica 13pt）")
        big_fonts(root, 13)
        app1 = snap(app, root)
        dump("13pt 1280", app1)
        check("右列随字体自动变宽（不是写死像素）",
              app1["right_w"] > app0["right_w"],
              "%d → %d" % (app0["right_w"], app1["right_w"]))
        check("右列宽度 ≠ 旧的硬编码 330", app1["right_w"] != 330,
              str(app1["right_w"]))
        check("变大字体后右列仍不裁剪",
              app1["right_w"] >= app1["inner"],
              "宽 %d / 需要 %d" % (app1["right_w"], app1["inner"]))
        check("变大字体后右列仍完整可见",
              app1["right_x"] + app1["right_w"] <= app1["body"],
              "x=%d + %d > %d" % (app1["right_x"], app1["right_w"],
                                  app1["body"]))
        check("左列让出空间但树仍完整（可伸缩）",
              app1["left_w"] >= app1["tree_req"],
              "宽 %d / 树需要 %d" % (app1["left_w"], app1["tree_req"]))

        print("\n--- C) 窄窗口（minsize 1000）")
        root.geometry("1000x640")
        app2 = snap(app, root)
        dump("13pt 1000", app2)
        check("窄窗下右列仍完整可见（不被挤扁）",
              app2["right_x"] + app2["right_w"] <= app2["body"],
              "x=%d + %d > %d" % (app2["right_x"], app2["right_w"],
                                  app2["body"]))
        check("窄窗下右列仍够装内容",
              app2["right_w"] >= app2["inner"],
              "宽 %d / 需要 %d" % (app2["right_w"], app2["inner"]))
        check("窄窗下左列没被压到 0", app2["left_w"] > 200, str(app2["left_w"]))

        print("\n--- D) pack 顺序（挤扁的根因）")
        body = app.right_col.master
        slaves = body.pack_slaves()
        idx_r = slaves.index(app.right_col)
        idx_l = slaves.index(app.tree.master)
        check("右列比左列先 pack（空间优先给右列）", idx_r < idx_l,
              "right=%d left=%d 顺序=%s"
              % (idx_r, idx_l, [s.winfo_class() for s in slaves]))
        check("两条进度条仍最先 pack（side=bottom 必须占位在先）",
              slaves[0] is app.pb_cur.master.master,
              "%s 不是进度条容器" % slaves[0])
    finally:
        root.destroy()
        shutil.rmtree(demo_dir, ignore_errors=True)

    print("\n通过 %d   失败 %d" % (OK, BAD))
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
