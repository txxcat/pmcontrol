# -*- coding: utf-8 -*-
"""「与设备同步」窗口的测试（数据逻辑 + 后端闭环 + 窗口结构）。

不连真机：用 FakeSync 代替 PlanSync，用临时目录代替 CH/。
表名一律 DEMO- 开头 —— 这个套件会被全量回归跑，不能碰真实频道表。

    py -3.12 _test_sync_gui.py
"""
import os
import shutil
import sys
import tempfile
import tkinter as tk

import promax_plan_edit as E
import promax_plan_editor_gui as EG
import promax_sync_gui as SG

OK, BAD, FAILS = 0, 0, []


def check(name, cond, extra=""):
    global OK, BAD
    if cond:
        OK += 1
    else:
        BAD += 1
        FAILS.append("%s %s" % (name, extra))


XML = ('<?xml version="1.0" encoding="utf-8"?>\n'
       '<CHANNEL-SET band="TERRESTRIAL" name="%s">'
       '<HEADER/><CHANNELS>%s</CHANNELS></CHANNEL-SET>\n')
CH = '<CHANNEL name="DEMO-CH%d" frequency="474.00" standard="DVB-C"/>'


def write_plan(src, kind, name, n=2):
    d = os.path.join(src, E.KIND_DIRS[kind])
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name + E.PLAN_EXT)
    with open(p, "w", encoding="utf-8") as f:
        f.write(XML % (name, "".join(CH % i for i in range(n))))
    return p


class FakeSync(object):
    """只实现 SyncWindow 会用到的那几个方法。"""

    host = "127.0.0.1"
    port = 2222
    sftp_port = 22
    transport = "auto"

    def __init__(self, src, names=None):
        self.src = src
        self.names = names or {"TER": ["DEMO-B.XML", "DEMO-C"],
                               "SAT": ["DEMO-SAT"]}
        self.closed = False
        self.log = lambda m: None
        self.uploaded = []
        self.downloaded = []

    def connect(self):
        return self

    def close(self):
        self.closed = True

    def device_plans(self, kind):
        return list(self.names.get(kind, []))

    def download(self, kind, name, dest=None, overwrite=True):
        dest = dest or E.plan_path(self.src, kind, SG.plain(name))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(XML % (SG.plain(name), CH % 0 + CH % 1 + CH % 2))
        self.downloaded.append((kind, name, dest))
        return dest

    def upload(self, path, kind=None, name=None):
        self.uploaded.append((path, kind, name))
        # 设备遇到同名不会覆盖，会自动改名（真机行为）
        return SG.plain(name) + "_2"


def part_data():
    """纯数据：规范化 / 三态 / 行文本 / 分组 / 没有删除入口。"""
    check("plain 去掉 .XML", SG.plain("DEMO-A.XML") == "DEMO-A")
    check("plain 兼容小写", SG.plain("DEMO-A.xml") == "DEMO-A")
    check("plain 不动不带后缀的", SG.plain("DEMO-A") == "DEMO-A")
    check("plain 保留尾随空格（真机表名要一字不差）",
          SG.plain("DEMO-A .XML") == "DEMO-A ")

    a = SG.PlanItem("TER", "DEMO-A", count=3, size=100)
    b = SG.PlanItem("TER", "DEMO-A.XML")
    c = SG.PlanItem("SAT", "DEMO-SAT")
    l = SG.PlanItem("TER", "DEMO-L", count=1)
    loc, dev = {a.key(): a, l.key(): l}, {b.key(): b, c.key(): c}
    check("两侧都有 = 同名", SG.status_of(a.key(), loc, dev) == SG.STATUS_SAME)
    check("只有本地 = 仅本地", SG.status_of(l.key(), loc, dev)
          == SG.STATUS_LOCAL)
    check("只有设备 = 仅设备", SG.status_of(c.key(), loc, dev)
          == SG.STATUS_DEVICE)
    check("本地行带频点数", "(3)" in SG.row_text(a, loc, dev))
    check("设备行不编频点数", "(3)" not in SG.row_text(b, loc, dev))
    check("设备行有 = 标记", SG.row_text(b, loc, dev).startswith("= "))
    check("仅本地行带 + 标记", SG.row_text(l, loc, dev).startswith("+ "))
    merged = dict(loc)
    merged.update(dev)
    g = SG.group_kinds(merged)
    check("分列：射频 2 / 卫星 1（同名 key 合并）",
          len(g["TER"]) == 2 and len(g["SAT"]) == 1)
    check("不提供删除设备表的入口", not hasattr(SG.SyncBackend, "remove"))


def part_backend():
    """后端闭环：预检分类 / 读设备 / 刷新本地 / 下载 / 上传 / 对比。"""
    src = tempfile.mkdtemp(prefix="pm_synctg_")
    made = []

    def factory():
        s = FakeSync(src)
        made.append(s)
        return s

    try:
        write_plan(src, "TER", "DEMO-A", 2)
        b = SG.SyncBackend(factory, src)
        check("初始重扫到本地表", ("TER", "DEMO-A") in b.local)
        check("本地项有频点数", b.local[("TER", "DEMO-A")].count == 2)
        check("读连接参数不产生已关闭的会话", not made[-1].closed)

        # 预检：把 socket 探测换成桩，测四种端口组合
        op, os_ = SG.SyncBackend._port_open, SG.SyncBackend._ssh_open
        try:
            for tel, sftp, expect in ((True, True, True),
                                      (False, False, False),
                                      (False, True, True),
                                      (True, False, True)):
                SG.SyncBackend._port_open = staticmethod(
                    lambda h, p, timeout=1.2, t=tel: t)
                SG.SyncBackend._ssh_open = staticmethod(
                    lambda h, p, timeout=1.2, s=sftp: s)
                r = b.probe()
                check("预检 2222=%s/22=%s → ok=%s" % (tel, sftp, expect),
                      r["ok"] is expect and r["telnet"] is tel
                      and r["sftp"] is sftp)
        finally:
            SG.SyncBackend._port_open, SG.SyncBackend._ssh_open = op, os_

        # 配置拿不到时归到 config，不假装端口不通
        bad = SG.SyncBackend(lambda: (_ for _ in ()).throw(
            RuntimeError("顶栏还没填仪器 IP")), src)
        check("配置错误单独归类", bad.probe()["kind"] == "config")

        ev = []
        emit = lambda x: ev.append(x)
        b.fetch_device(emit)
        check("设备表带 .XML 也被规范化", ("TER", "DEMO-B") in b.device)
        check("设备表不带 .XML 也读到了", ("TER", "DEMO-C") in b.device)
        check("设备项不编频点数", b.device[("TER", "DEMO-B")].count is None)
        check("读设备清单写了日志", any(e[0] == "log" for e in ev))
        check("会话用完关掉了", made[-1].closed)

        # 刷新本地：目录里新增一张表后能感知到
        write_plan(src, "SAT", "DEMO-NEW", 1)
        check("刷新本地发现新表", b.rescan_local() == 1)
        check("刷新本地第二遍无变化", b.rescan_local() == 0)
        check("新表进了本地清单", ("SAT", "DEMO-NEW") in b.local)

        # 下载闭环：设备独有 → 本地
        dl = SG.PlanItem("TER", "DEMO-C")
        r = b.download([dl], emit)
        check("下载成功 1 张", r["ok"] == 1 and not r["fail"])
        check("下载后本地多了这张", ("TER", "DEMO-C") in b.local)
        check("下载有进度事件", any(e[0] == "progress" for e in ev))

        # 上传闭环：本地表 → 设备（返回自动改名后的真名）
        up = b.local[("TER", "DEMO-A")]
        r = b.upload([up], emit)
        check("上传成功 1 张", r["ok"] == 1 and not r["fail"])
        check("上传带 .XML 后缀", made[-1].uploaded[-1][2] == "DEMO-A.XML")
        check("自动改名按真名记账", ("TER", "DEMO-A_2") in b.device)

        # 对比：设备表临时下载后解析出频点数
        cmp_item = SG.PlanItem("TER", "DEMO-B")
        res = b.compare(cmp_item, emit)
        check("对比拿到设备侧内容", res["device"] is not None)
        check("对比解析出 3 个频点",
              res["device"] is not None and res["device"].count == 3)
        check("对比不抛错", res["error"] == "")
        no_dev = b.compare(SG.PlanItem("TER", "DEMO-NOPE"), emit)
        check("设备上没有的表给出说明", "设备上没有" in no_dev["error"])
    finally:
        shutil.rmtree(src, ignore_errors=True)


def part_window():
    """窗口结构：四个分列 Listbox、无删除按钮、选中互斥、刷新本地。"""
    src = tempfile.mkdtemp(prefix="pm_synctg_win_")
    root = None
    win = None
    saved = (SG.SyncWindow.connect, SG.messagebox.showinfo,
             SG.messagebox.showerror, SG.messagebox.askyesnocancel)
    try:
        write_plan(src, "TER", "DEMO-A", 2)
        write_plan(src, "SAT", "DEMO-S", 1)

        # 不联网、不弹模态框
        SG.SyncWindow.connect = lambda self, retry=0: None
        SG.messagebox.showinfo = lambda *a, **k: None
        SG.messagebox.showerror = lambda *a, **k: None
        SG.messagebox.askyesnocancel = lambda *a, **k: None

        root = tk.Tk()
        root.withdraw()
        made = []

        def factory():
            s = FakeSync(src)
            made.append(s)
            return s

        win = SG.SyncWindow(root, factory, src)
        win.backend.device = {
            ("TER", "DEMO-B"): SG.PlanItem("TER", "DEMO-B"),
            ("SAT", "DEMO-T"): SG.PlanItem("SAT", "DEMO-T"),
        }
        win.connected = True
        win._refresh_lists()

        check("四个分列 Listbox", len(win.lists) == 4)
        check("本地射频列有表", win.lists[("local", "TER")].size() == 1)
        check("本地卫星列有表", win.lists[("local", "SAT")].size() == 1)
        check("设备射频列有表", win.lists[("device", "TER")].size() == 1)
        check("设备卫星列有表", win.lists[("device", "SAT")].size() == 1)
        labels = [str(b.cget("text")) for b in win.buttons.values()]
        check("中间没有删除按钮", not any("删除" in x for x in labels))
        check("中间有下载按钮", any("下载到本地" in x for x in labels))
        check("中间有上传按钮", any("上传到设备" in x for x in labels))
        check("左右都有刷新按钮",
              "刷新本地" in [str(w.cget("text"))
                            for w in _all_buttons(win)]
              and any("刷新设备" in str(w.cget("text"))
                      for w in _all_buttons(win)))

        # 选一侧 → 另一侧自动清空
        win.lists[("local", "TER")].selection_set(0)
        win._on_pick("local")
        win.lists[("device", "TER")].selection_set(0)
        win._on_pick("device")
        check("选设备侧会清掉本地侧的选择",
              not win.lists[("local", "TER")].curselection())
        check("设备侧选中仍在", bool(win.lists[("device", "TER")].curselection()))

        # 刷新本地：目录里新增一张表，界面要跟着变
        write_plan(src, "TER", "DEMO-FRESH", 1)
        win.refresh_local()
        check("刷新本地后界面多一行",
              win.lists[("local", "TER")].size() == 2)
        check("刷新本地不触发联网", len(made) == 1)
    except Exception as e:
        check("窗口测试没抛异常", False, repr(e))
    finally:
        try:
            if win is not None:
                win.destroy()
            if root is not None:
                root.destroy()
        except Exception:
            pass
        (SG.SyncWindow.connect, SG.messagebox.showinfo,
         SG.messagebox.showerror, SG.messagebox.askyesnocancel) = saved
        shutil.rmtree(src, ignore_errors=True)


def part_editor_entry():
    """主程序入口：界面上只剩一个「与设备同步…」，且能打开新窗口。"""
    src = tempfile.mkdtemp(prefix="pm_synctg_ent_")
    root = editor = None
    saved = (SG.SyncWindow.connect, SG.messagebox.showinfo,
             SG.messagebox.showerror, EG.messagebox.showinfo)
    try:
        write_plan(src, "TER", "DEMO-A", 1)
        SG.SyncWindow.connect = lambda self, retry=0: None
        SG.messagebox.showinfo = lambda *a, **k: None
        SG.messagebox.showerror = lambda *a, **k: None
        EG.messagebox.showinfo = lambda *a, **k: None

        class F(object):
            host, port, sftp_port, transport = "127.0.0.1", 2222, 22, "auto"
            log = lambda self, m: None
            def connect(self): return self
            def close(self): pass
            def device_plans(self, kind): return []

        root = tk.Tk()
        root.withdraw()
        editor = EG.PlanEditorWindow(root, src, sync_factory=lambda: F())
        labels = [str(w.cget("text")) for w in _all_buttons(editor)]
        check("界面上有新入口", "与设备同步…" in labels)
        check("旧三个按钮已撤下",
              not any(x in labels for x in
                      ("设备上的表…", "从设备下载…", "上传到设备…")), labels)
        editor.open_sync_window()
        w = getattr(editor, "_sync_win", None)
        check("打开的是同步窗口", isinstance(w, SG.SyncWindow))
        check("新窗口有四个分列列表",
              w is not None and len(w.lists) == 4)
        editor.open_sync_window()
        check("重复点击复用同一窗口", editor._sync_win is w)
    except Exception as e:
        check("编辑器入口测试没抛异常", False, repr(e))
    finally:
        try:
            if editor is not None:
                editor.destroy()
            if root is not None:
                root.destroy()
        except Exception:
            pass
        (SG.SyncWindow.connect, SG.messagebox.showinfo,
         SG.messagebox.showerror, EG.messagebox.showinfo) = saved
        shutil.rmtree(src, ignore_errors=True)


def _all_buttons(w):
    out = []

    def walk(x):
        for c in x.winfo_children():
            if c.winfo_class() == "TButton":
                out.append(c)
            walk(c)
    walk(w)
    return out


def main():
    part_data()
    part_backend()
    part_window()
    part_editor_entry()
    print("\n%s\n通过 %d / 失败 %d" % ("=" * 46, OK, BAD))
    if FAILS:
        print("失败项：")
        for f in FAILS:
            print("  - %s" % f)
    return 0 if BAD == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
