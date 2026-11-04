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
import zipfile

import promax_plan_backup as B
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
        self.removed = []
        self.events = []
        # 故障注入：按规范化表名匹配
        self.fail_download = set()
        self.empty_download = set()
        self.fail_remove = set()

    def connect(self):
        return self

    def close(self):
        self.closed = True

    def device_plans(self, kind):
        return list(self.names.get(kind, []))

    def download(self, kind, name, dest=None, overwrite=True):
        key = SG.plain(name)
        self.events.append(("download", key))
        if key in self.fail_download:
            raise RuntimeError("模拟：设备读取失败")
        dest = dest or E.plan_path(self.src, kind, key)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        body = "" if key in self.empty_download else (
            XML % (key, CH % 0 + CH % 1 + CH % 2))
        with open(dest, "w", encoding="utf-8") as f:
            f.write(body)
        self.downloaded.append((kind, name, dest))
        return dest

    def upload(self, path, kind=None, name=None):
        self.uploaded.append((path, kind, name))
        # 设备遇到同名不会覆盖，会自动改名（真机行为）
        return SG.plain(name) + "_2"

    def remove(self, kind, name):
        key = SG.plain(name)
        self.events.append(("remove", key))
        if key in self.fail_remove:
            raise RuntimeError("模拟：设备拒绝删除")
        self.removed.append((kind, name))
        return True


def part_data():
    """纯数据：规范化 / 三态 / 行文本 / 分组 / 删除入口只有「选中项」。"""
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
    check("删除只提供「选中项」方法", hasattr(SG.SyncBackend, "remove_selected"))
    check("没有「删除全部」的后端入口",
          not hasattr(SG.SyncBackend, "remove_all")
          and not hasattr(SG.SyncBackend, "remove"))
    check("删除有专用确认框", hasattr(SG, "DeleteConfirmDialog"))


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


def part_backup_files():
    """backup_files：原样字节、不解析、manifest 带 SHA256、滚动保留。"""
    d = tempfile.mkdtemp(prefix="pm_synctg_bk_")
    try:
        # 故意用本地解析器认不出的内容：backup_plans 会跳过，backup_files 必须保住
        raw = b'<?xml version="1.0"?><NOT-LOCAL-FORMAT foo="1"/>'
        p = os.path.join(d, "raw.dat")
        with open(p, "wb") as f:
            f.write(raw)
        z = B.backup_files([(p, "CH/TER/DEMO-Raw.XML")], app_dir=d,
                           tag="pre-delete", reason="删除设备表前备份 1 张")
        check("备份 zip 已落盘", bool(z) and os.path.isfile(z))
        check("备份文件名带 pre-delete",
              z and "pre-delete" in os.path.basename(z))
        with zipfile.ZipFile(z) as f:
            names = f.namelist()
            check("不解析也照样备份",
                  "CH/TER/DEMO-Raw.XML" in names)
            check("内容与原字节一致",
                  f.read("CH/TER/DEMO-Raw.XML") == raw)
            man = f.read(B.MANIFEST).decode("utf-8", "replace")
            check("manifest 里有 SHA256", "SHA256" in man)
            check("manifest 里写了触发原因", "删除设备表前备份" in man)
            check("manifest 里写了恢复办法", "还原备份" in man)
        check("空列表不生成 zip", B.backup_files([], app_dir=d) is None)
        check("不存在的文件被忽略",
              B.backup_files([(os.path.join(d, "nope"), "CH/TER/X.XML")],
                             app_dir=d) is None)
        for i in range(12):
            B.backup_files([(p, "CH/TER/DEMO-Raw.XML")], app_dir=d,
                           tag="r%d" % i)
        check("滚动只留最近 10 份",
              len(B.list_backups(d)) == B.BACKUP_KEEP)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def part_remove():
    """删除设备表：备份是前置条件 / 只删选中 / 失败一张都不删。"""
    src = tempfile.mkdtemp(prefix="pm_synctg_del_")
    made = []
    ctl = {"fail_download": set(), "empty_download": set(),
           "fail_remove": set()}

    def factory():
        s = FakeSync(src)
        s.fail_download = set(ctl["fail_download"])
        s.empty_download = set(ctl["empty_download"])
        s.fail_remove = set(ctl["fail_remove"])
        made.append(s)
        return s

    emit = lambda x: None
    TWO = (("TER", "DEMO-B"), ("SAT", "DEMO-SAT"))

    def fresh():
        b = SG.SyncBackend(factory, src, backup_app_dir=src)
        b.fetch_device(emit)
        return b

    def pick(b, keys):
        return [b.device[k] for k in keys]

    try:
        # ---- 1. 正常：先全部备份，再删除 ----
        b = fresh()
        r = b.remove_selected(pick(b, TWO), emit)
        s = made[-1]
        check("删除成功 2 张",
              r["ok"] == 2 and not r["fail"] and not r["aborted"])
        check("删除前备份已生成",
              bool(r["backup"]) and os.path.isfile(r["backup"]))
        with zipfile.ZipFile(r["backup"]) as z:
            names = z.namelist()
            check("备份里两张表都在",
                  "CH/TER/DEMO-B.XML" in names
                  and "CH/SAT/DEMO-SAT.XML" in names)
            check("备份保留了设备原文",
                  z.read("CH/TER/DEMO-B.XML").startswith(b"<?xml"))
        kinds = [e[0] for e in s.events]
        dls = [i for i, k in enumerate(kinds) if k == "download"]
        rms = [i for i, k in enumerate(kinds) if k == "remove"]
        check("两张都先下载后删除",
              len(dls) == 2 and len(rms) == 2 and max(dls) < min(rms))
        check("删掉的表移出设备清单",
              ("TER", "DEMO-B") not in b.device
              and ("SAT", "DEMO-SAT") not in b.device)
        check("没选中的表留在设备清单", ("TER", "DEMO-C") in b.device)
        check("删除会话用完关掉", s.closed)

        # ---- 2. 任一表下载失败 → 一张都不删 ----
        ctl["fail_download"] = {"DEMO-B"}
        b = fresh()
        r = b.remove_selected(pick(b, TWO), emit)
        check("下载失败 → aborted", r["aborted"] is True)
        check("下载失败 → 没删成功也没生成备份",
              r["ok"] == 0 and r["backup"] is None)
        check("下载失败 → 设备上一张都没删", not made[-1].removed)
        check("下载失败 → 清单不变",
              ("TER", "DEMO-B") in b.device
              and ("SAT", "DEMO-SAT") in b.device)
        check("失败原因写明是备份失败",
              any("备份失败" in e for _, e in r["fail"]))

        # ---- 3. 下载到空文件同样中止（不能拿空文件当备份）----
        ctl["fail_download"] = set()
        ctl["empty_download"] = {"DEMO-SAT"}
        b = fresh()
        r = b.remove_selected(pick(b, TWO), emit)
        check("空文件也算备份失败", r["aborted"] and r["backup"] is None)
        check("空文件 → 一张都没删", not made[-1].removed)

        # ---- 4. 打包失败 → 一张都不删 ----
        ctl["empty_download"] = set()
        orig = SG.B.backup_files

        def boom(*a, **k):
            raise OSError("模拟：磁盘满")

        try:
            SG.B.backup_files = boom
            b = fresh()
            r = b.remove_selected(pick(b, TWO), emit)
        finally:
            SG.B.backup_files = orig
        check("打包失败 → aborted", r["aborted"] is True)
        check("打包失败 → 一张都没删", not made[-1].removed)
        check("打包失败 → 清单不变", ("TER", "DEMO-B") in b.device)

        # ---- 5. 备份在手，单张删除失败不影响其余 ----
        ctl["fail_remove"] = {"DEMO-B"}
        b = fresh()
        r = b.remove_selected(pick(b, TWO), emit)
        check("部分失败：成功 1 张 / 失败 1 张",
              r["ok"] == 1 and len(r["fail"]) == 1)
        check("部分失败仍有备份",
              bool(r["backup"]) and os.path.isfile(r["backup"]))
        check("删失败的表仍在清单", ("TER", "DEMO-B") in b.device)
        check("删成功的表移出清单", ("SAT", "DEMO-SAT") not in b.device)

        # ---- 6. 只删选中的那一张 ----
        ctl["fail_remove"] = set()
        b = fresh()
        r = b.remove_selected(pick(b, [("TER", "DEMO-B")]), emit)
        check("只删选中的 1 张",
              len(made[-1].removed) == 1 and r["ok"] == 1)
        check("其余两张没被碰",
              ("TER", "DEMO-C") in b.device
              and ("SAT", "DEMO-SAT") in b.device)
        empty = b.remove_selected([], emit)
        check("空选择不动设备", empty["ok"] == 0 and not empty["aborted"])
    finally:
        shutil.rmtree(src, ignore_errors=True)


def part_confirm():
    """删除确认框：必须主动勾选才放行，取消不置 confirmed。"""
    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        items = [SG.PlanItem("TER", "DEMO-A"), SG.PlanItem("SAT", "DEMO-S")]
        d = SG.DeleteConfirmDialog(root, items)
        check("确认框初始删除按钮禁用",
              str(d.ok_btn.cget("state")) == "disabled")
        d.ack.set(True)
        d._on_ack()
        check("勾选后删除按钮启用",
              str(d.ok_btn.cget("state")) == "normal")
        d._ok()
        check("点删除 → confirmed", d.confirmed is True)
        d2 = SG.DeleteConfirmDialog(root, items)
        d2._cancel()
        check("取消 → 不 confirmed", d2.confirmed is False)
    except Exception as e:
        check("确认框测试没抛异常", False, repr(e))
    finally:
        try:
            if root is not None:
                root.destroy()
        except Exception:
            pass


def part_window():
    """窗口结构：四个分列 Listbox、删除按钮启用规则、选中互斥、刷新本地。"""
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
        check("中间有「删除选中…」按钮", "删除选中…" in labels)
        check("没有「删除全部 / 删除所有」按钮",
              not any(x in labels for x in ("全部删除", "删除所有", "清空设备")))
        check("中间有下载按钮", any("下载到本地" in x for x in labels))
        check("中间有上传按钮", any("上传到设备" in x for x in labels))
        check("左右都有刷新按钮",
              "刷新本地" in [str(w.cget("text"))
                            for w in _all_buttons(win)]
              and any("刷新设备" in str(w.cget("text"))
                      for w in _all_buttons(win)))
        check("没有选中时删除按钮禁用",
              str(win.buttons["remove"].cget("state")) == "disabled")

        # 中栏加了删除按钮后，最小窗口高度必须仍装得下全部按钮与图例
        win.update_idletasks()
        mid = win.buttons["remove"].master
        chrome = sum(c.winfo_reqheight() for c in win.winfo_children()
                     if c.winfo_class() in ("TFrame", "TLabel")
                     and c is not mid.master)
        check("最小窗口高度装得下中栏",
              win.minsize()[1] >= mid.winfo_reqheight() + chrome,
              "mid=%d chrome=%d min=%d" % (mid.winfo_reqheight(), chrome,
                                           win.minsize()[1]))

        # 选一侧 → 另一侧自动清空；删除按钮只认设备侧的选择
        win.lists[("local", "TER")].selection_set(0)
        win._on_pick("local")
        check("只选本地时删除按钮禁用",
              str(win.buttons["remove"].cget("state")) == "disabled")
        win.lists[("device", "TER")].selection_set(0)
        win._on_pick("device")
        check("设备侧有选中时删除按钮启用",
              str(win.buttons["remove"].cget("state")) == "normal")
        check("选设备侧会清掉本地侧的选择",
              not win.lists[("local", "TER")].curselection())
        check("设备侧选中仍在", bool(win.lists[("device", "TER")].curselection()))

        # 右键菜单：只有设备侧给删除入口
        win.menu.tk_popup = lambda x, y: None

        class Ev(object):
            x_root = y_root = 0
            y = 5

        def menu_labels(side, kind):
            win._context(Ev(), side, kind, win.lists[(side, kind)])
            out = []
            for i in range(win.menu.index("end") + 1):
                if win.menu.type(i) != "separator":
                    out.append(str(win.menu.entrycget(i, "label")))
            return out

        check("设备侧右键有删除", any("删除" in x for x in
                                     menu_labels("device", "TER")))
        win.lists[("device", "TER")].selection_clear(0, "end")
        win.lists[("local", "TER")].selection_set(0)
        check("本地侧右键没有删除", not any("删除" in x for x in
                                           menu_labels("local", "TER")))

        # 取消确认 → 不启动任何后台任务
        win._ask_delete = lambda items: False
        win.lists[("local", "TER")].selection_clear(0, "end")
        win.lists[("device", "TER")].selection_set(0)
        win._on_pick("device")
        win.on_remove()
        check("取消删除后不进入 busy", not win.busy)

        # 备份失败的结果：明确告知「一张都没删」
        errs = []
        SG.messagebox.showerror = lambda *a, **k: errs.append(a)
        win._on_remove_result({"ok": 0, "fail": [("DEMO-B", "备份失败")],
                               "backup": None, "aborted": True})
        check("备份失败弹「未执行删除」",
              any("未执行删除" in str(a) for a in errs))
        check("备份失败状态栏说明没删表",
              "一张表都没有删除" in win.status_var.get())

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
    part_backup_files()
    part_remove()
    part_confirm()
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
