# -*- coding: utf-8 -*-
"""「与设备同步」窗口 —— 一个窗口完成连接、左右对照、下载 / 上传。

取代 `promax_plan_editor_gui` 里原来的三个按钮（设备上的表 / 下载 / 上传）：
打开窗口就做**短超时端口预检 + 读设备清单**，然后本地与设备并排显示，
每侧再分射频（TER）/ 卫星（SAT）两列。

设计约束（沿用项目铁律）
------------------------
· **跨线程**：worker 只往 `queue.Queue` 塞事件，绝不碰 Tk（连 after 都不行）；
  主线程 `after` 轮询取事件再刷界面。
· **每次任务一个会话**：`sync_factory()` 造出的 PlanSync 用完就 close，
  和主程序 `_run_bg` 一致 —— 仪器是单会话设备，别把 socket 跨线程长期复用。
· **深色模式**：Listbox / Text 用 `promax_theme` 显式配色，ttk 跟随系统。
· **覆盖本地前自动备份**：走 `promax_plan_backup.backup_plans()`，
  与保存 / 删除 / 改名 / 还原同一套规矩。
· **不提供删除设备表的入口**：设备侧没有回滚命令，太危险。
· **状态只有三态**：设备清单只有表名，拿不到频点数 / 大小 / 时间，
  所以列表里只标「同名 / 仅本地 / 仅设备」。同名**不代表内容相同**，
  要看内容就点「对比…」—— 那会把设备那张下载到临时目录再解析。

数据纪律：源码里不写任何真实 IP / 表名 / 频率；仪器 IP 只在运行时从
`sync_factory()` 造出的会话对象上读取。
"""
import datetime
import os
import queue
import shutil
import socket
import tempfile
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import promax_fileio as FIO
import promax_plan_backup as B
import promax_plan_edit as E
import promax_theme as theme

HINT = theme.c("hint")
DANGER = theme.c("danger")
KINDS = E.KINDS
KIND_LABEL = E.KIND_LABEL
KIND_DIRS = E.KIND_DIRS
PROBE_TIMEOUT = 1.2              # 端口预检单次超时；只试一次，不自动反复重连

# 行首标记。文字是主依据 —— 选中行的 selectforeground 会盖掉 itemconfig 的颜色。
STATUS_SAME = "同名"
STATUS_LOCAL = "仅本地"
STATUS_DEVICE = "仅设备"
MARK = {STATUS_SAME: "= ", STATUS_LOCAL: "+ ", STATUS_DEVICE: "> ", "": "  "}


# ============================================================ 数据模型
def plain(name):
    """规范化表名：去掉 .XML（大小写不敏感），**不 strip**。

    ⚠️ 真机有表名带尾随空格 / 不带 .XML 的情况，下载时名字要一字不差，
       所以这里只动扩展名，别顺手 strip。
    """
    s = str(name or "")
    if s.upper().endswith(E.PLAN_EXT.upper()):
        return s[:-len(E.PLAN_EXT)]
    return s


class PlanItem(object):
    """列表里的一张表。

    本地项有 count / size / mtime / path；设备侧只有名字（清单给不出别的），
    对应字段为 None —— 界面按 None 决定显示什么，不编数据。
    """

    def __init__(self, kind, name, count=None, size=None, mtime=None,
                 path=None, error=""):
        self.kind = kind
        self.name = plain(name)
        self.count = count
        self.size = size
        self.mtime = mtime
        self.path = path
        self.error = error or ""

    def key(self):
        return (self.kind, self.name)

    @property
    def filename(self):
        return self.name + E.PLAN_EXT

    @property
    def label(self):
        return "%s · %s" % (KIND_LABEL[self.kind], self.filename)


def status_of(key, local, device):
    """两侧对照：同名 / 仅本地 / 仅设备。"""
    a, b = key in local, key in device
    if a and b:
        return STATUS_SAME
    if a:
        return STATUS_LOCAL
    if b:
        return STATUS_DEVICE
    return ""


def group_kinds(data):
    """按射频 / 卫星分成两列的数据。"""
    out = {k: [] for k in KINDS}
    for it in sorted(data.values(), key=lambda x: (x.kind != KINDS[0], x.name)):
        out[it.kind].append(it)
    return out


def row_text(it, local, device):
    mark = MARK.get(status_of(it.key(), local, device), "  ")
    if it.count is None:
        return "%s%s" % (mark, it.filename)
    return "%s%s  (%d)" % (mark, it.filename, it.count)


# ============================================================ 真机后端
class SyncBackend(object):
    """把 PlanSync 包成窗口需要的五个动作。

    所有**联网**方法都在 worker 线程里被调用；`rescan_local()` 是纯本地
    重扫，主线程直接调。改完 `local / device` 后由窗口在 result 事件里刷新。
    """

    def __init__(self, sync_factory, source):
        self.sync_factory = sync_factory
        self.source = os.path.abspath(source)
        self.local = {}
        self.device = {}
        self.cfg_error = ""
        self.host = ""
        self.telnet_port = 2222
        self.sftp_port = 22
        self.transport = "auto"
        # sync_factory() 只造对象、不连设备，可以从它身上读连接参数
        try:
            cfg = sync_factory()
            self.host = getattr(cfg, "host", "") or ""
            self.telnet_port = int(getattr(cfg, "port", 2222) or 2222)
            self.sftp_port = int(getattr(cfg, "sftp_port", 22) or 22)
            self.transport = getattr(cfg, "transport", "auto") or "auto"
        except Exception as e:
            self.cfg_error = str(e)
        self.rescan_local()

    def rescan_local(self):
        """重扫 CH/TER、CH/SAT，返回新增张数（真机版：没有演示造数据）。"""
        before = set(self.local)
        out = {}
        for it in E.list_plans(self.source):
            p = it["path"]
            row = PlanItem(it["kind"], it["name"], count=it["count"],
                           size=os.path.getsize(p) if os.path.isfile(p) else None,
                           mtime=it["mtime"], path=p, error=it["error"])
            out[row.key()] = row
        self.local = out
        return len(set(out) - before)

    # ---------------------------------------------------------- 连接预检
    def probe(self):
        """短超时探 22 / 2222 两个端口，只试一次，绝不自动反复重连。

        22 还要读一次 banner：端口开着但对面不是 SSH 时不能算 SFTP 可用。
        """
        t0 = time.time()
        if self.cfg_error:
            return {"ok": False, "kind": "config", "detail": self.cfg_error,
                    "host": "", "telnet": False, "sftp": False,
                    "cost": time.time() - t0, "mode": "—"}
        telnet = self._port_open(self.host, self.telnet_port)
        sftp = self._ssh_open(self.host, self.sftp_port)
        ok = telnet or sftp
        mode = ("SFTP（端口 %d）" % self.sftp_port if sftp
                else "Telnet（端口 %d）" % self.telnet_port)
        return {"ok": ok, "kind": "probe", "detail": "",
                "host": self.host, "telnet": telnet, "sftp": sftp,
                "cost": time.time() - t0, "mode": mode,
                "transport": self.transport}

    @staticmethod
    def _port_open(host, port, timeout=PROBE_TIMEOUT):
        try:
            s = socket.create_connection((host, port), timeout=timeout)
            s.close()
            return True
        except OSError:
            return False

    @staticmethod
    def _ssh_open(host, port, timeout=PROBE_TIMEOUT):
        try:
            s = socket.create_connection((host, port), timeout=timeout)
            try:
                s.settimeout(timeout)
                return s.recv(64).startswith(b"SSH-")
            finally:
                s.close()
        except OSError:
            return False

    # ---------------------------------------------------------- 会话
    def _new_sync(self, emit):
        """造一个只属于本次任务的会话，并把内部日志接到窗口日志区。"""
        sync = self.sync_factory()
        try:
            sync.log = lambda m: emit(("log", str(m)))
        except Exception:
            pass
        return sync

    @staticmethod
    def _close(sync):
        try:
            sync.close()
        except Exception:
            pass

    # ---------------------------------------------------------- 读设备清单
    def fetch_device(self, emit):
        emit(("log", "读取设备上的频道表清单…"))
        sync = self._new_sync(emit)
        try:
            sync.connect()
            out = {}
            for kind in KINDS:
                for name in (sync.device_plans(kind) or []):
                    it = PlanItem(kind, name)
                    out[it.key()] = it
            self.device = out
        finally:
            self._close(sync)
        g = group_kinds(self.device)
        emit(("log", "设备清单读取完成：射频 %d 张 / 卫星 %d 张"
              % (len(g[KINDS[0]]), len(g[KINDS[1]]))))
        return dict(self.device)

    # ---------------------------------------------------------- 下载 / 上传
    def download(self, items, emit):
        """下载到 source/TER|SAT/<name>.XML；单张失败不拖累其余。"""
        n, bad = 0, []
        sync = self._new_sync(emit)
        try:
            sync.connect()
            for i, it in enumerate(items):
                emit(("progress", i, len(items), "下载 %s（%d/%d）"
                      % (it.filename, i + 1, len(items))))
                try:
                    dest = E.plan_path(self.source, it.kind, it.name)
                    sync.download(it.kind, it.name, dest, overwrite=True)
                    n += 1
                    emit(("log", "← 已下载 %s · %s"
                          % (KIND_LABEL[it.kind], it.filename)))
                except Exception as e:
                    bad.append((it.filename, str(e)))
                    emit(("log", "✘ 下载失败 %s：%s" % (it.filename, e)))
        finally:
            self._close(sync)
        emit(("progress", len(items), len(items), "完成"))
        self.rescan_local()
        return {"ok": n, "fail": bad}

    def upload(self, items, emit):
        """上传本地表；设备遇到同名不会覆盖、会自动改名，按返回真名记账。"""
        n, bad = 0, []
        sync = self._new_sync(emit)
        try:
            sync.connect()
            for i, it in enumerate(items):
                emit(("progress", i, len(items), "上传 %s（%d/%d）"
                      % (it.filename, i + 1, len(items))))
                try:
                    # 传带 .XML 的名字：Telnet 单表命令只认带扩展名的表名
                    got = sync.upload(it.path, kind=it.kind, name=it.filename)
                    real = PlanItem(it.kind, got or it.name)
                    self.device[real.key()] = real
                    n += 1
                    emit(("log", "→ 已上传 %s · %s（设备上存为 %s）"
                          % (KIND_LABEL[it.kind], it.filename, real.filename)))
                except Exception as e:
                    bad.append((it.filename, str(e)))
                    emit(("log", "✘ 上传失败 %s：%s" % (it.filename, e)))
        finally:
            self._close(sync)
        emit(("progress", len(items), len(items), "完成"))
        return {"ok": n, "fail": bad}

    # ---------------------------------------------------------- 内容对比
    def compare(self, item, emit):
        """把设备那张表下载到临时目录解析，与本地同名表比频点数 / 大小。

        只针对用户选中的一张表，不做全表预取 —— 设备清单本身给不出大小。
        """
        emit(("log", "读取设备上的 %s 用于对比…" % item.filename))
        tmp = tempfile.mkdtemp(prefix="pm_cmp_")
        result = {"key": item.key(), "local": self.local.get(item.key()),
                  "device": None, "error": ""}
        sync = None
        try:
            if item.key() in self.device:
                sync = self._new_sync(emit)
                try:
                    sync.connect()
                    dest = os.path.join(tmp, item.filename)
                    sync.download(item.kind, item.name, dest, overwrite=True)
                    try:
                        count = len(E.load_plan(dest))
                    except Exception:
                        count = None
                    result["device"] = PlanItem(
                        item.kind, item.name, count=count,
                        size=os.path.getsize(dest) if os.path.isfile(dest) else None)
                except Exception as e:
                    result["error"] = str(e)
            else:
                result["error"] = "设备上没有这张表"
        finally:
            if sync is not None:
                self._close(sync)
            shutil.rmtree(tmp, ignore_errors=True)
        return result


# ============================================================ 断线提示窗
class ConnectFailDialog(tk.Toplevel):
    """连不上时的提示窗 —— 只弹一次，不自动反复重试。

    choice：'retry' / 'offline' / 'exit'
    """

    def __init__(self, parent, probe, retry):
        tk.Toplevel.__init__(self, parent)
        self.choice = "exit"
        self.title("无法连接仪器")
        self.transient(parent)
        self.resizable(False, False)
        self.grab_set()

        kind = probe.get("kind")
        tel, sftp = probe.get("telnet"), probe.get("sftp")
        detail = probe.get("detail") or ""
        if kind == "config":
            title = "同步配置不可用"
            reason = "拿不到仪器连接参数：\n%s" % detail
            tips = ["· 回主窗口顶栏填好仪器 IP 和端口",
                    "· 设置 → 同步页里确认传输方式与 SFTP 账号",
                    "· 改好后点本窗口的「刷新设备 F5」"]
        elif kind == "silent":
            title = "仪器没有应答"
            reason = ("端口能连上，但一个字节都不回。\n"
                      "这是「会话被占住 / 远程控制服务卡死」，不是命令写错了。\n"
                      "再多重试几次只会让单会话的仪器更不响应。")
            tips = ["· 上一会话可能还挂着：等 30~60 秒再试",
                    "· 确认另一台电脑 / 厂家遥控软件没有占着连接",
                    "· 用 webControl 的 Reboot Equipment，或断电重启"]
            if detail:
                tips.append("· 设备原话：%s" % detail.splitlines()[0][:60])
        elif kind == "error":
            title = "读取设备清单失败"
            reason = "端口通了，但读表清单时出错：\n%s" % detail[:200]
            tips = ["· 确认仪器固件与传输方式（自动 / SFTP / Telnet）匹配",
                    "· 2222 服务卡死时重启仪器；22 通的话优先走 SFTP",
                    "· 仍不行把操作日志里的设备原话发出来"]
        elif not tel and not sftp:
            title = "没检测到仪器"
            reason = ("端口 %d 和 %d 都连不上。\n"
                      "可能是网线没接、IP 填错、或者仪器没开机。"
                      % (probe.get("sftp_port", 22), probe.get("telnet_port", 2222)))
            tips = ["· 检查仪器 IP 和网线，确认接的是 IPCTRL 口",
                    "· 用浏览器开 http://<仪器IP>/ 看 webControl 能不能进",
                    "· 仍不行就断电重启仪器一次"]
        else:
            title = "远程控制通道不可用"
            reason = ("%d 不通，但 %d（SFTP）还活着。"
                      % (probe.get("telnet_port", 2222), probe.get("sftp_port", 22)))
            tips = ["· 设置 → 同步页把传输方式改成 SFTP，可直接读写频道表",
                    "· 2222 服务卡死时，重启仪器即可恢复"]

        f = ttk.Frame(self, padding=(18, 14))
        f.pack(fill="both", expand=True)
        ttk.Label(f, text=title, font=("TkDefaultFont", 10, "bold"),
                  foreground=DANGER).pack(anchor="w", pady=(0, 10))
        ttk.Label(f, text=reason, justify="left", wraplength=430).pack(
            anchor="w", pady=(0, 12))
        ttk.Label(f, text="怎么办", font=("TkDefaultFont", 9, "bold")).pack(
            anchor="w")
        for t in tips:
            ttk.Label(f, text=t, justify="left", wraplength=430,
                      foreground=HINT).pack(anchor="w", pady=1)

        ttk.Separator(f).pack(fill="x", pady=12)
        ttk.Label(f, text="预检耗时 %.2f 秒 · 第 %d 次尝试 · 没有反复重连"
                  % (probe.get("cost", 0), retry + 1),
                  foreground=HINT).pack(anchor="w", pady=(0, 10))
        ttk.Label(f, text="目标：%s   端口 %d=%s / %d=%s"
                  % (probe.get("host", "") or "（未配置）",
                     probe.get("sftp_port", 22), "通" if sftp else "不通",
                     probe.get("telnet_port", 2222), "通" if tel else "不通"),
                  foreground=HINT).pack(anchor="w", pady=(0, 12))

        bf = ttk.Frame(f)
        bf.pack(fill="x")
        ttk.Button(bf, text="退出", command=self._exit).pack(side="left")
        ttk.Button(bf, text="仅看本地（离线）",
                   command=self._offline).pack(side="left", padx=6)
        ttk.Button(bf, text="重试连接", command=self._retry).pack(side="right")
        self.bind("<Return>", lambda e: self._retry())
        self.bind("<Escape>", lambda e: self._exit())
        self.protocol("WM_DELETE_WINDOW", self._exit)
        self.update_idletasks()
        try:
            x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 2
            self.geometry("+%d+%d" % (max(0, x), max(0, y)))
        except Exception:
            pass

    def _retry(self):
        self.choice = "retry"
        self.destroy()

    def _offline(self):
        self.choice = "offline"
        self.destroy()

    def _exit(self):
        self.choice = "exit"
        self.destroy()


# ============================================================ 对比对话框
class CompareDialog(tk.Toplevel):
    """同名表两侧的字段对比（设备侧按需要临时下载解析）。"""

    def __init__(self, parent, result):
        tk.Toplevel.__init__(self, parent)
        kind, name = result["key"]
        self.title("对比：%s" % name)
        self.transient(parent)
        self.resizable(False, False)
        f = ttk.Frame(self, padding=(16, 12))
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="%s · %s%s" % (KIND_LABEL[kind], name, E.PLAN_EXT),
                  font=("TkDefaultFont", 10, "bold")).pack(anchor="w", pady=(0, 10))

        a, b = result["local"], result["device"]
        cols = ("字段", "本地", "设备")
        tv = ttk.Treeview(f, columns=cols, show="headings", height=6)
        for c, w in zip(cols, (110, 170, 170)):
            tv.heading(c, text=c)
            tv.column(c, width=w, anchor="w")
        rows = [
            ("是否存在", "有" if a else "无", "有" if b else "无"),
            ("频点数", str(a.count) if a and a.count is not None else "—",
             str(b.count) if b and b.count is not None else "—"),
            ("大小 (字节)", str(a.size) if a and a.size is not None else "—",
             str(b.size) if b and b.size is not None else "—"),
            ("修改时间", _fmt_time(a.mtime) if a else "—", "—"),
            ("结论", _compare_verdict(a, b, result["error"])),
        ]
        for i, r in enumerate(rows):
            tv.insert("", "end", iid=str(i), values=r)
        tv.pack(fill="x")
        if result["error"]:
            ttk.Label(f, text=result["error"], foreground=HINT,
                      wraplength=430, justify="left").pack(anchor="w", pady=(8, 0))
        ttk.Label(f, text="同名不代表内容相同；这里按频点数与文件大小粗比。",
                  foreground=HINT).pack(anchor="w", pady=(8, 0))
        ttk.Button(f, text="关闭", command=self.destroy).pack(anchor="e", pady=(12, 0))
        self.grab_set()
        self.bind("<Escape>", lambda e: self.destroy())


def _fmt_time(ts):
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "—"


def _compare_verdict(a, b, error):
    if error:
        return "无法对比（%s）" % error[:20]
    if not a or not b:
        return "只有一侧有"
    if a.count is not None and b.count is not None and a.count != b.count:
        return "频点数不同"
    if a.size is not None and b.size is not None and a.size != b.size:
        return "文件大小不同"
    if a.count is None or b.count is None:
        return "设备内容读不出来"
    return "粗判一致"


# ============================================================ 主窗口
class SyncWindow(tk.Toplevel):
    """与设备同步：打开即连接 → 读设备表 → 左右对照 → 中间操作。"""

    def __init__(self, master, sync_factory, source, on_change=None,
                 on_close=None):
        tk.Toplevel.__init__(self, master)
        self.on_change = on_change
        self.on_close = on_close
        self.title("与设备同步")
        self.geometry("1180x780")
        self.minsize(1060, 660)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.transient(master)

        self.backend = SyncBackend(sync_factory, source)
        self.q = queue.Queue()
        self.busy = False
        self.connected = False
        self.offline = False
        self.retry = 0
        self.state_var = tk.StringVar(value="准备连接")
        self.info_var = tk.StringVar(value="")
        self.left_stat = tk.StringVar(value="")
        self.right_stat = tk.StringVar(value="")
        self.progress_var = tk.StringVar(value="就绪")
        self.status_var = tk.StringVar(value="就绪")
        self.lists = {}               # (side, kind) -> tk.Listbox，共 4 个
        self.rows = {}                # (side, kind) -> [PlanItem]
        self.buttons = {}
        self.log_open = True
        self._picking = False
        self._pending_fail = None
        self._last_probe = None
        self.menu = tk.Menu(self, tearoff=0)

        self._build()
        self.after(180, lambda: self.connect(0))

    # ------------------------------------------------------------ 布局
    def _build(self):
        top = ttk.Frame(self, padding=(10, 8, 10, 4))
        top.pack(fill="x")
        ttk.Label(top, text="频道表目录：").pack(side="left")
        ttk.Label(top, text=self.backend.source, foreground=HINT).pack(side="left")
        ttk.Button(top, text="关闭", command=self.close).pack(side="right")
        ttk.Button(top, text="断开", command=self.disconnect
                   ).pack(side="right", padx=6)

        sub = ttk.Frame(self, padding=(10, 0, 10, 6))
        sub.pack(fill="x")
        self.state_lbl = ttk.Label(sub, textvariable=self.state_var,
                                   foreground=HINT)
        self.state_lbl.pack(side="left")
        ttk.Label(sub, textvariable=self.info_var,
                  foreground=HINT).pack(side="left", padx=10)

        body = ttk.Frame(self, padding=(10, 0, 10, 6))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(2, weight=1)
        body.rowconfigure(0, weight=1)

        left = ttk.LabelFrame(body, text="本地频道表", padding=6)
        mid = ttk.Frame(body, padding=(4, 0))
        right = ttk.LabelFrame(body, text="设备上的频道表", padding=6)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        mid.grid(row=0, column=1, sticky="ns")
        right.grid(row=0, column=2, sticky="nsew", padx=(6, 0))
        self._build_side(left, "local")
        self._build_side(right, "device")
        self._build_middle(mid)

        bottom = ttk.Frame(self, padding=(10, 0, 10, 4))
        bottom.pack(fill="x")
        pr = ttk.Frame(bottom)
        pr.pack(fill="x")
        self.pb = ttk.Progressbar(pr, mode="determinate", maximum=100, length=180)
        self.pb.pack(side="left")
        ttk.Label(pr, textvariable=self.progress_var,
                  foreground=HINT).pack(side="left", padx=10)
        ttk.Button(pr, text="收起日志", width=10,
                   command=self._toggle_log).pack(side="right")

        self.log_frame = ttk.LabelFrame(bottom, text="操作日志", padding=4)
        self.log_frame.pack(fill="x", pady=(6, 0))
        self.log_text = tk.Text(self.log_frame, height=5, wrap="word",
                                **theme.text_opts())
        self.log_text.pack(side="left", fill="x", expand=True)
        sb = ttk.Scrollbar(self.log_frame, orient="vertical",
                           command=self.log_text.yview)
        sb.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=sb.set, state="disabled")

        self.status_frame = ttk.Frame(bottom)
        self.status_frame.pack(fill="x", pady=(6, 0))
        ttk.Label(self, textvariable=self.status_var, anchor="w",
                  padding=(10, 2)).pack(fill="x", side="bottom")

        self.bind("<F5>", lambda e: self.refresh_device())
        self.bind("<Escape>", lambda e: self.close())

    def _build_side(self, pane, side):
        row = ttk.Frame(pane)
        row.pack(fill="both", expand=True)
        for kind in KINDS:
            if side == "local":
                title = "%s（%s）—— %s/" % (KIND_LABEL[kind], kind,
                                           KIND_DIRS[kind])
            else:
                title = "%s（%s）" % (KIND_LABEL[kind], kind)
            box = ttk.LabelFrame(row, text=title, padding=6)
            box.pack(side="left", fill="both", expand=True, padx=(0, 6))
            # 🔴 4 个 Listbox 共存：exportselection=False，否则选中一个会清掉
            #    另一个的高亮（跨平台 X selection 冲突）。
            lb = tk.Listbox(box, width=26, height=14, exportselection=False,
                            activestyle="none", selectmode="extended",
                            **theme.listbox_opts())
            lb.pack(fill="both", expand=True)
            lb.bind("<<ListboxSelect>>", lambda ev, s=side: self._on_pick(s))
            lb.bind("<Double-1>", lambda ev, s=side: (
                self.on_download() if s == "device" else self.on_upload()))
            lb.bind("<Button-3>", lambda ev, s=side, k=kind, w=lb:
                    self._context(ev, s, k, w))
            lb.bind("<Control-a>", lambda ev, s=side, k=kind, w=lb:
                    self._select_all(w, s, k))
            lb.bind("<Control-A>", lambda ev, s=side, k=kind, w=lb:
                    self._select_all(w, s, k))
            self.lists[(side, kind)] = lb
            self.rows[(side, kind)] = []

        bar = ttk.Frame(pane)
        bar.pack(fill="x", pady=(6, 0))
        if side == "local":
            ttk.Button(bar, text="刷新本地", command=self.refresh_local
                       ).pack(side="left")
        else:
            ttk.Button(bar, text="刷新设备 F5", command=self.refresh_device
                       ).pack(side="left")
        ttk.Button(bar, text="全选", command=lambda: self._select_side(side, True)
                   ).pack(side="left", padx=4)
        ttk.Button(bar, text="清除选择",
                   command=lambda: self._select_side(side, False)
                   ).pack(side="left")

        ttk.Label(pane, textvariable=(
            self.left_stat if side == "local" else self.right_stat),
            foreground=HINT, anchor="w").pack(fill="x", pady=(6, 0))
        ttk.Label(pane, text="Ctrl / Shift 多选 · Ctrl+A 全选 · 双击执行",
                  foreground=HINT, anchor="w").pack(fill="x")

    def _build_middle(self, mid):
        self.buttons["download"] = ttk.Button(
            mid, text="← 下载到本地", width=15, command=self.on_download)
        self.buttons["download"].pack(pady=(2, 2))
        self.buttons["upload"] = ttk.Button(
            mid, text="上传到设备 →", width=15, command=self.on_upload)
        self.buttons["upload"].pack(pady=(8, 2))

        ttk.Separator(mid).pack(fill="x", pady=12)
        self.buttons["compare"] = ttk.Button(
            mid, text="对比…", width=15, command=self.on_compare)
        self.buttons["compare"].pack(pady=2)
        self.buttons["down_all"] = ttk.Button(
            mid, text="全部下载", width=15,
            command=lambda: self.on_all("down"))
        self.buttons["down_all"].pack(pady=2)
        self.buttons["up_all"] = ttk.Button(
            mid, text="全部上传", width=15,
            command=lambda: self.on_all("up"))
        self.buttons["up_all"].pack(pady=2)

        ttk.Separator(mid).pack(fill="x", pady=12)
        ttk.Label(mid, text="标记", foreground=HINT).pack(anchor="w")
        for txt in ("=  两侧同名", "+  仅本地有", ">  仅设备有"):
            ttk.Label(mid, text=txt, foreground=HINT,
                      justify="left").pack(anchor="w", pady=1)
        ttk.Label(mid, text="\n选一侧会自动清掉\n另一侧的选择，\n按钮随之启用。\n\n"
                            "同名不代表内容\n相同，要看内容\n点「对比…」。",
                  justify="left", foreground=HINT).pack(
                      anchor="w", pady=(10, 0))

    # ------------------------------------------------------------ 小工具
    def _log(self, msg):
        try:
            t = datetime.datetime.now().strftime("%H:%M:%S")
            self.log_text.configure(state="normal")
            self.log_text.insert("end", "[%s] %s\n" % (t, msg))
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        except tk.TclError:
            pass

    def _toggle_log(self):
        self.log_open = not self.log_open
        if self.log_open:
            self.log_frame.pack(fill="x", pady=(6, 0),
                                before=self.status_frame)
        else:
            self.log_frame.pack_forget()

    def _set_state(self, text, fg="hint"):
        self.state_var.set(text)
        self.state_lbl.configure(foreground=HINT if fg == "hint" else DANGER)

    def _clear_other(self, side):
        other = "device" if side == "local" else "local"
        was = self._picking
        self._picking = True
        try:
            for k in KINDS:
                self.lists[(other, k)].selection_clear(0, "end")
        finally:
            self._picking = was

    def _on_pick(self, side):
        if self._picking:
            return
        self._clear_other(side)
        self._update_actions()

    def _select_all(self, lb, side, kind):
        if lb.size() and self.rows[(side, kind)]:
            lb.selection_set(0, "end")
        self._clear_other(side)
        self._update_actions()
        return "break"

    def _select_side(self, side, on):
        was = self._picking
        self._picking = True
        try:
            for k in KINDS:
                lb = self.lists[(side, k)]
                lb.selection_clear(0, "end")
                if on and lb.size() and self.rows[(side, k)]:
                    lb.selection_set(0, "end")
        finally:
            self._picking = was
        if on:
            self._clear_other(side)
        self._update_actions()

    def _selected(self, side):
        out = []
        for kind in KINDS:
            lb, rows = self.lists[(side, kind)], self.rows[(side, kind)]
            for i in lb.curselection():
                if i < len(rows):
                    out.append(rows[i])
        return out

    def _refresh_side(self, side, keep=None):
        keep = set(keep or ())
        data = self.backend.local if side == "local" else self.backend.device
        loc, dev = self.backend.local, self.backend.device
        groups = group_kinds(data)
        was = self._picking
        self._picking = True
        try:
            for kind in KINDS:
                lb = self.lists[(side, kind)]
                lb.delete(0, "end")
                rows = []
                if side == "device" and not self.connected:
                    if kind == KINDS[0]:
                        lb.insert("end", "（离线：未读取设备）"
                                  if self.offline else "正在读取设备…")
                        lb.itemconfigure(0, foreground=HINT)
                else:
                    for it in groups[kind]:
                        idx = lb.size()
                        lb.insert("end", row_text(it, loc, dev))
                        if it.error:
                            lb.itemconfigure(idx, foreground=DANGER)
                        rows.append(it)
                        if it.key() in keep:
                            lb.selection_set(idx)
                self.rows[(side, kind)] = rows
        finally:
            self._picking = was
        self._update_stats(side)

    def _update_stats(self, side):
        data = self.backend.local if side == "local" else self.backend.device
        loc, dev = self.backend.local, self.backend.device
        stat = self.left_stat if side == "local" else self.right_stat
        if side == "device" and not self.connected:
            stat.set("未连接设备 · 点「刷新设备 F5」重试（本地表仍可查看）")
            return
        nsel = sum(len(self.lists[(side, k)].curselection()) for k in KINDS)
        want = STATUS_LOCAL if side == "local" else STATUS_DEVICE
        only = sum(1 for k in data if status_of(k, loc, dev) == want)
        g = group_kinds(data)
        stat.set("%d 张（%s %d / %s %d）· 已选 %d · %s %d"
                 % (len(data), KIND_LABEL[KINDS[0]], len(g[KINDS[0]]),
                    KIND_LABEL[KINDS[1]], len(g[KINDS[1]]), nsel, want, only))

    def _refresh_lists(self):
        for side in ("local", "device"):
            keep = [it.key() for it in self._selected(side)]
            self._refresh_side(side, keep)
        loc, dev = self.backend.local, self.backend.device
        keys = set(loc) | set(dev)
        same = sum(1 for k in keys if status_of(k, loc, dev) == STATUS_SAME)
        dl = sum(1 for k in keys if status_of(k, loc, dev) == STATUS_DEVICE)
        ul = sum(1 for k in keys if status_of(k, loc, dev) == STATUS_LOCAL)
        self.status_var.set("合计 %d 张 · 同名 %d · 待下载 %d · 待上传 %d"
                            % (len(keys), same, dl, ul))
        self._update_actions()

    def _update_actions(self):
        busy, conn = self.busy, self.connected
        ndev, nloc = len(self._selected("device")), len(self._selected("local"))
        state = {
            "download": conn and not busy and ndev > 0,
            "upload": conn and not busy and nloc > 0,
            "compare": not busy and ((ndev + nloc) == 1 or
                                     (ndev == 1 and nloc == 1)),
            "down_all": conn and not busy and any(
                status_of(k, self.backend.local, self.backend.device) == STATUS_DEVICE
                for k in self.backend.device),
            "up_all": conn and not busy and any(
                status_of(k, self.backend.local, self.backend.device) == STATUS_LOCAL
                for k in self.backend.local),
        }
        for name, ok in state.items():
            self.buttons[name].configure(state="normal" if ok else "disabled")
        for side in ("local", "device"):
            self._update_stats(side)

    def _context(self, ev, side, kind, lb):
        i = lb.nearest(ev.y)
        rows = self.rows[(side, kind)]
        if i < 0 or i >= len(rows):
            return
        if i not in lb.curselection():
            was = self._picking
            self._picking = True
            try:
                lb.selection_clear(0, "end")
                lb.selection_set(i)
            finally:
                self._picking = was
        self._clear_other(side)
        self._update_actions()
        m = self.menu
        m.delete(0, "end")
        m.add_command(label="← 下载到本地" if side == "device"
                      else "上传到设备 →",
                      command=self.on_download if side == "device"
                      else self.on_upload)
        m.add_command(label="对比同名表", command=self.on_compare)
        m.tk_popup(ev.x_root, ev.y_root)

    def _set_busy(self, on):
        self.busy = on
        self._update_actions()
        if on:
            self.pb.configure(value=0)
            self.progress_var.set("准备中…")

    def _notify(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    # ------------------------------------------------------------ 后台任务
    def _start(self, fn, *args):
        if self.busy:
            return
        self._set_busy(True)

        def emit(ev):
            self.q.put(ev)

        def worker():
            try:
                result = fn(*args, emit=emit)
                self.q.put(("result", result))
            except Exception as e:
                self.q.put(("error", str(e)))
            finally:
                self.q.put(("finished",))

        threading.Thread(target=worker, daemon=True).start()
        self.after(60, self._poll)

    def _poll(self):
        try:
            alive = bool(self.winfo_exists())
        except tk.TclError:
            alive = False
        if not alive:
            self.busy = False
            return
        try:
            while True:
                self._handle(self.q.get_nowait())
        except queue.Empty:
            pass
        if self.busy:
            self.after(60, self._poll)

    def _handle(self, ev):
        kind = ev[0]
        if kind == "log":
            self._log(ev[1])
        elif kind == "progress":
            _, done, total, text = ev
            self.pb.configure(maximum=max(1, total), value=done)
            self.progress_var.set("%d/%d  %s" % (done, total, text))
        elif kind == "connect_ok":
            _, probe, cost, retry = ev
            self.connected, self.offline = True, False
            self._set_state("已连接", "hint")
            self.info_var.set("目标 %s · 传输 %s · 预检 %.2f 秒 · 第 %d 次连接"
                              % (probe["host"], probe["mode"], cost, retry + 1))
            self._log("连接成功")
            self._refresh_lists()
            self.status_var.set("已连接，可以开始同步")
        elif kind == "connect_fail":
            self._pending_fail = ev[1]
            self._set_state("未连接", "danger")
            self.status_var.set("连接失败 —— 等待用户选择（不会自动重连）")
        elif kind == "error":
            self.status_var.set("失败：%s" % ev[1])
            messagebox.showerror("操作失败", ev[1], parent=self)
            self._log("失败：%s" % ev[1])
        elif kind == "result":
            payload = ev[1]
            if isinstance(payload, tuple) and payload[0] == "compare":
                CompareDialog(self, payload[1])
            elif isinstance(payload, tuple) and payload[0] == "task":
                _, verb, res = payload
                ok, bad = res.get("ok", 0), res.get("fail", [])
                self.status_var.set("%s完成：成功 %d 张，失败 %d 张"
                                    % (verb, ok, len(bad)))
                self._log("%s完成：成功 %d 张，失败 %d 张" % (verb, ok, len(bad)))
                self._refresh_lists()
                if ok:
                    self._notify()
                if bad:
                    lines = ["成功 %d 张，失败 %d 张" % (ok, len(bad)), ""]
                    lines += ["  %s —— %s" % (n, e) for n, e in bad]
                    messagebox.showwarning("%s结果（部分失败）" % verb,
                                           "\n".join(lines), parent=self)
        elif kind == "finished":
            self._set_busy(False)
            self.progress_var.set("就绪")
            if self._pending_fail is not None:
                probe = self._pending_fail
                self._pending_fail = None
                self._ask_fail(probe)

    # ------------------------------------------------------------ 连接
    def connect(self, retry=0):
        self.retry = retry
        self.connected = False
        self.offline = False
        self._set_state("正在检测仪器…", "hint")
        self.info_var.set("目标 %s · 端口预检（单次超时 %.1f 秒）"
                          % (self.backend.host or "（未配置）", PROBE_TIMEOUT))
        self._log("— 开始连接（第 %d 次）" % (retry + 1))
        self._refresh_side("device")
        self._update_actions()
        self._start(self._connect_job, retry)

    def _connect_job(self, retry, emit):
        emit(("log", "端口预检：%d / %d，单次超时 %.1f 秒…"
              % (self.backend.sftp_port, self.backend.telnet_port,
                 PROBE_TIMEOUT)))
        probe = self.backend.probe()
        probe.update(sftp_port=self.backend.sftp_port,
                     telnet_port=self.backend.telnet_port)
        self._last_probe = probe
        emit(("log", "预检结果：%d=%s，%d=%s（%.2f 秒）"
              % (self.backend.sftp_port, "通" if probe["sftp"] else "不通",
                 self.backend.telnet_port, "通" if probe["telnet"] else "不通",
                 probe["cost"])))
        if not probe["ok"]:
            emit(("connect_fail", probe))
            return None
        try:
            self.backend.fetch_device(emit)
        except FIO.PromaxSilent as e:
            p = dict(probe, kind="silent", detail=str(e))
            emit(("log", "仪器没有应答 —— 停下等用户决定，不自动重连"))
            emit(("connect_fail", p))
            return None
        except Exception as e:
            p = dict(probe, kind="error", detail=str(e))
            emit(("log", "读取设备清单失败：%s" % e))
            emit(("connect_fail", p))
            return None
        emit(("connect_ok", probe, probe["cost"], retry))
        return None

    def _ask_fail(self, probe):
        dlg = ConnectFailDialog(self, probe, self.retry)
        self.wait_window(dlg)
        if dlg.choice == "retry":
            self._log("用户选择：重试连接")
            self.connect(self.retry + 1)
        elif dlg.choice == "offline":
            self.offline = True
            self._set_state("离线（仅本地）", "hint")
            self.info_var.set("未连接设备 · 只显示本地表 · 点「刷新设备 F5」可重试")
            self._log("用户选择：离线浏览")
            self._refresh_lists()
        else:
            self._log("用户选择：退出")
            self.close()

    def disconnect(self):
        if self.busy:
            return messagebox.showinfo("忙", "还有任务在跑，请稍等", parent=self)
        self.connected = False
        self.offline = True
        self._set_state("已断开", "hint")
        self.info_var.set("已断开 · 点「刷新设备 F5」重新连接")
        self._log("已断开连接")
        self._refresh_lists()

    # ------------------------------------------------------------ 两个刷新
    def refresh_device(self):
        """刷新设备 = 重新做端口预检 + 重读设备清单（联网，可能失败）。"""
        if self.busy:
            return
        self.connect(0)

    def refresh_local(self):
        """刷新本地 = 只重扫 CH/TER、CH/SAT（纯本地，离线也能用）。

        频道表管理窗口可以在本窗口开着时新建 / 改名 / 另存 / 删除 / 还原备份，
        不同步就会拿着过期清单上传，甚至传一张已被删掉的表。
        """
        if self.busy:
            return
        n = self.backend.rescan_local()
        self._log("刷新本地：重扫频道表目录 —— %s"
                  % ("发现 %d 张新表" % n if n else "无变化"))
        self.status_var.set("本地表已刷新（新增 %d 张）" % n)
        self._refresh_lists()

    # ------------------------------------------------------------ 操作
    def _confirm_dup(self, items, target_is_device):
        """同名一次问清。设备侧不会覆盖、会自动改名，话术不能写「覆盖」。"""
        target = self.backend.device if target_is_device else self.backend.local
        dup = [it for it in items if it.key() in target]
        if not dup:
            return list(items)
        if target_is_device:
            detail = ("设备不会覆盖同名文件 —— 重复上传会被自动改名。\n\n"
                      "是 = 仍按原名上传（设备自动改名）\n"
                      "否 = 跳过同名，只传其余\n取消 = 都不做")
        else:
            detail = ("是 = 覆盖本地同名表（覆盖前自动备份）\n"
                      "否 = 跳过同名，只下载其余\n取消 = 都不做")
        ans = messagebox.askyesnocancel(
            "有 %d 张同名表" % len(dup),
            "目标侧已存在 %d 张同名表：\n%s\n\n%s"
            % (len(dup), "、".join(x.filename for x in dup[:6]), detail),
            parent=self)
        if ans is None:
            return None
        if ans is False:
            return [it for it in items if it.key() not in target]
        return list(items)

    def _backup_before_overwrite(self, items):
        """覆盖本地同名表之前先整份备份（与保存/删除同一套规矩）。"""
        if not any(it.key() in self.backend.local for it in items):
            return True
        try:
            B.backup_plans(self.backend.source, tag="pre-download",
                           reason="覆盖下载前")
            return True
        except OSError as e:
            return messagebox.askyesno(
                "备份失败", "下载前自动备份失败：\n%s\n\n仍然继续下载？" % e,
                parent=self)

    def on_download(self):
        items = self._selected("device")
        if not items:
            return messagebox.showinfo("下载", "先在右边选设备上的表", parent=self)
        items = self._confirm_dup(items, False)
        if items is None:
            return
        if not items:
            return messagebox.showinfo("下载", "没有需要下载的表", parent=self)
        if not self._backup_before_overwrite(items):
            return
        self._log("下载 %d 张（设备 → 本地）" % len(items))
        self._start(self._task_job, "down", items, "下载")

    def on_upload(self):
        items = self._selected("local")
        if not items:
            return messagebox.showinfo("上传", "先在左边选本地的表", parent=self)
        bad = [it for it in items if not it.path or not os.path.isfile(it.path)]
        if bad:
            return messagebox.showerror(
                "上传", "这些表在本地已找不到（点「刷新本地」更新清单）：\n%s"
                % "、".join(x.filename for x in bad), parent=self)
        items = self._confirm_dup(items, True)
        if items is None:
            return
        if not items:
            return messagebox.showinfo("上传", "没有需要上传的表", parent=self)
        self._log("上传 %d 张（本地 → 设备）" % len(items))
        self._start(self._task_job, "up", items, "上传")

    def on_all(self, direction):
        loc, dev = self.backend.local, self.backend.device
        want = STATUS_DEVICE if direction == "down" else STATUS_LOCAL
        src = dev if direction == "down" else loc
        items = [it for k, it in src.items()
                 if status_of(k, loc, dev) == want]
        if not items:
            return messagebox.showinfo(
                "提示", "没有%s的表" % ("需要下载" if direction == "down" else "需要上传"),
                parent=self)
        verb = "全部下载" if direction == "down" else "全部上传"
        if not messagebox.askyesno(verb, "%s %d 张一侧独有的表？"
                                   % (verb, len(items)), parent=self):
            return
        if direction == "down" and not self._backup_before_overwrite(items):
            return
        self._log("%s：%d 张" % (verb, len(items)))
        self._start(self._task_job, direction, items, verb)

    def on_compare(self):
        sel = self._selected("device") or self._selected("local")
        if len(sel) != 1:
            return messagebox.showinfo("对比", "请只选一张表（左右都行）", parent=self)
        if not self.connected:
            return messagebox.showinfo("对比", "要先连上设备才能读设备侧内容",
                                       parent=self)
        self._log("对比 %s（临时读取设备上的表）" % sel[0].filename)
        self._start(self._compare_job, sel[0])

    def _task_job(self, direction, items, verb, emit):
        res = (self.backend.download(items, emit) if direction == "down"
               else self.backend.upload(items, emit))
        return ("task", verb, res)

    def _compare_job(self, item, emit):
        return ("compare", self.backend.compare(item, emit))

    # ------------------------------------------------------------ 关闭
    def close(self):
        try:
            if not self.winfo_exists():
                if self.on_close:
                    self.on_close()
                return
        except tk.TclError:
            return
        if self.busy and not messagebox.askyesno(
                "还有任务在跑", "后台任务还没结束，确定关闭？", parent=self):
            return
        self.destroy()
        if self.on_close:
            self.on_close()
