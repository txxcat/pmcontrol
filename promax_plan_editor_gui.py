# -*- coding: utf-8 -*-
"""频道表编辑器窗口：左侧列表（射频/卫星两个文件夹）+ 右侧频点表格。

设计要点
--------
· **一个窗口管两张表列表**：射频在 `CH/TER/`、卫星在 `CH/SAT/`，一眼能看出
  哪张表属于哪个频段（仪器也是这么分的）。
· 打开一张表就载入整棵 XML 树，编辑只改被动的那个节点 —— 未知字段
  （`SAT@band`、`LNB`、`BAND_KUKA`…）原样保留，见 `promax_plan_edit` 的说明。
· **保存前自动备份**：整份 CH 目录打成 zip 放进 `bak/`（滚动 10 份），
  另外随时可以手动点「备份」。
· 设备同步放后台线程：下载/上传可能要几十秒，界面不能假死。

窗口不自己连设备：它只拿到一个"同步会话工厂"，由主窗口传进来（好测、也好换）。
"""
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import promax_plan_backup as B
import promax_plan_edit as E

# 表格列：内部键 → (表头, 宽度, 对齐)
COLS = (("idx", "#", 44, "center"),
        ("name", "频道名", 150, "w"),
        ("freq", "频率 (MHz)", 110, "e"),
        ("standard", "制式", 96, "center"),
        ("symb", "符号率", 84, "e"),
        ("sat", "卫星", 104, "center"))

STD_LABEL = {
    "DVB-C": "DVB-C", "DVB-C2": "DVB-C2", "DVB-T": "DVB-T",
    "DVB-T2": "DVB-T2", "ANALOGTER": "模拟", "FM": "FM", "GENERIC": "通用",
    "DVB-S": "DVB-S", "DVB-S2": "DVB-S2", "DSS": "DSS",
}


class PlanEditorWindow(tk.Toplevel):
    """频道表管理（新建 / 编辑 / 删除 / 备份 / 与设备同步）"""

    def __init__(self, master, source, sync_factory=None, on_change=None):
        tk.Toplevel.__init__(self, master)
        self.title("频道表管理")
        self.transient(master)
        self.geometry("1180x760")
        # 最小宽度要够「左列两个列表 444 + 右列频点表 6 列 588」，
        # 再窄就得靠横向滚动条翻列了（能翻，但一次看不全）
        self.minsize(1060, 620)
        self.source = os.path.abspath(source)
        self.sync_factory = sync_factory      # callable() -> PlanSync
        self.on_change = on_change            # 表有变动时通知主窗口重扫
        self.plan = None                      # 当前 PlanFile
        self.plans = {"TER": [], "SAT": []}   # 列表项
        self._busy = False
        self._sync = None
        self._q = queue.Queue()          # 后台任务结果队列（见 _run_bg）
        self._bg_after = None            # 轮询定时器 id（关窗要取消）
        self._bg_done_fn = None

        self._build()
        self.refresh_lists()
        self._update_state()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------ 界面
    def _build(self):
        top = ttk.Frame(self, padding=(10, 8, 10, 4))
        top.pack(fill="x")
        ttk.Label(top, text="频道表目录：").pack(side="left")
        self.dir_var = tk.StringVar(value=self.source)
        ttk.Label(top, textvariable=self.dir_var,
                  foreground="#555").pack(side="left")
        ttk.Button(top, text="备份全部（zip）", command=self.backup_all
                   ).pack(side="right")
        ttk.Button(top, text="还原备份…", command=self.restore_backup
                   ).pack(side="right", padx=6)

        body = ttk.Frame(self, padding=(10, 0, 10, 6))
        body.pack(fill="both", expand=True)

        # ---- 左：两个列表（上下两块：列表区 + 按钮条）----
        # 🔴 按钮条必须装在**自己的行**里再竖直排列。若把两个 LabelFrame 直接
        #    side="left" 塞进 left、按钮条随后 pack，按钮会落到"列表右边的剩余区域"，
        #    看着像一排按钮飘在列表右侧（实测 1180 宽时右列被挤到只剩 324px）。
        left = ttk.Frame(body)
        left.pack(side="left", fill="y")

        row = ttk.Frame(left)
        row.pack(fill="both", expand=True)

        self.lists = {}
        for kind, title in ((E.KIND_TER, "射频（TER）"),
                            (E.KIND_SAT, "卫星（SAT）")):
            box = ttk.LabelFrame(row, text="%s —— %s/"
                                 % (title, E.KIND_DIRS[kind]), padding=6)
            box.pack(side="left", fill="both", expand=True, padx=(0, 6))
            lb = tk.Listbox(box, width=28, height=22, exportselection=False,
                            activestyle="none")
            lb.pack(fill="both", expand=True)
            lb.bind("<<ListboxSelect>>", lambda e, k=kind: self._on_pick(k))
            self.lists[kind] = lb

        lbtn = ttk.Frame(left)
        lbtn.pack(fill="x", pady=(6, 0))
        ttk.Button(lbtn, text="新建表…", command=self.new_plan
                   ).pack(side="left")
        ttk.Button(lbtn, text="重命名…", command=self.rename_plan
                   ).pack(side="left", padx=4)
        ttk.Button(lbtn, text="复制…", command=self.copy_plan
                   ).pack(side="left")
        ttk.Button(lbtn, text="删除表", command=self.delete_plan
                   ).pack(side="left", padx=4)

        # ---- 右：频点表格 ----
        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)

        self.head_var = tk.StringVar(value="（左侧选一张表）")
        ttk.Label(right, textvariable=self.head_var,
                  font=("TkDefaultFont", 10, "bold")).pack(anchor="w")

        cols = tuple(c[0] for c in COLS)
        # 滚动条先 pack（先拿自己的宽度/高度），表格后 pack 吃掉剩下的 —— 否则窄窗时
        # 表格会被压到滚动条位置上去（pack 按顺序分配空间）。
        # 两个方向都配：窗口再窄也不会"看不见最后一列"。
        treebox = ttk.Frame(right)
        treebox.pack(fill="both", expand=True, pady=(4, 0))
        sby = ttk.Scrollbar(treebox, orient="vertical")
        sby.pack(side="right", fill="y")
        sbx = ttk.Scrollbar(treebox, orient="horizontal")
        sbx.pack(side="bottom", fill="x")
        self.tree = ttk.Treeview(treebox, columns=cols, show="headings",
                                 height=18, selectmode="browse")
        self.tree.pack(side="left", fill="both", expand=True)
        for key, title, w, anchor in COLS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=w, anchor=anchor,
                             stretch=(key == "name"))
        sby.configure(command=self.tree.yview)
        sbx.configure(command=self.tree.xview)
        self.tree.configure(yscrollcommand=sby.set, xscrollcommand=sbx.set)
        self.tree.bind("<Double-1>", lambda e: self.edit_channel())

        cbtn = ttk.Frame(right)
        cbtn.pack(fill="x", pady=(6, 0))
        for text, cmd in (("新增频点…", self.add_channel),
                          ("编辑…", self.edit_channel),
                          ("删除", self.del_channel),
                          ("上移", lambda: self.move_channel(-1)),
                          ("下移", lambda: self.move_channel(1)),
                          ("按频率排序", self.sort_channels)):
            ttk.Button(cbtn, text=text, command=cmd).pack(side="left", padx=(0, 4))

        save = ttk.Frame(right)
        save.pack(fill="x", pady=(8, 0))
        self.save_btn = ttk.Button(save, text="保存（先自动备份）",
                                   command=self.save_plan)
        self.save_btn.pack(side="left")
        ttk.Button(save, text="另存为…", command=self.save_as
                   ).pack(side="left", padx=4)
        ttk.Button(save, text="放弃改动", command=self.reload_plan
                   ).pack(side="left")

        self.dirty_var = tk.StringVar(value="")
        ttk.Label(save, textvariable=self.dirty_var,
                  foreground="#c00").pack(side="left", padx=8)

        # ---- 设备同步 ----
        sync = ttk.LabelFrame(right, text="与设备同步", padding=6)
        sync.pack(fill="x", pady=(10, 0))
        ttk.Button(sync, text="设备上的表…", command=self.show_device_plans
                   ).pack(side="left")
        ttk.Button(sync, text="从设备下载…", command=self.download_from_device
                   ).pack(side="left", padx=4)
        ttk.Button(sync, text="上传当前表到设备", command=self.upload_current
                   ).pack(side="left")
        self.sync_var = tk.StringVar(value="")
        ttk.Label(sync, textvariable=self.sync_var,
                  foreground="#555").pack(side="left", padx=10)

        # ---- 状态栏 ----
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self, textvariable=self.status_var, anchor="w",
                  padding=(10, 2)).pack(fill="x", side="bottom")

    # ------------------------------------------------------------ 列表
    def refresh_lists(self, keep=None):
        """重扫两张表列表。keep = (kind, name) 保留选中"""
        self.plans = {"TER": [], "SAT": []}
        for it in E.list_plans(self.source):
            self.plans[it["kind"]].append(it)
        for kind in E.KINDS:
            lb = self.lists[kind]
            lb.delete(0, "end")
            for it in self.plans[kind]:
                mark = "!! " if it["error"] else ""
                lb.insert("end", "%s%s  (%d)" % (mark, it["name"], it["count"]))
        if keep:
            kind, name = keep
            for i, it in enumerate(self.plans[kind]):
                if it["name"] == name:
                    self.lists[kind].selection_clear(0, "end")
                    self.lists[kind].selection_set(i)
                    self.lists[kind].see(i)
                    break

    def _sel(self, kind=None):
        """当前选中的表（先看指定频段，再看另一个）"""
        for k in ([kind] if kind else []) + list(E.KINDS):
            lb = self.lists[k]
            cur = lb.curselection()
            if cur:
                i = cur[0]
                if i < len(self.plans[k]):
                    return self.plans[k][i]
        return None

    def _on_pick(self, kind):
        it = self._sel(kind)
        if not it:
            return
        # 另一个列表取消选中（避免两个同时高亮，看不出在编哪张）
        for k in E.KINDS:
            if k != kind:
                self.lists[k].selection_clear(0, "end")
        self._load(it)

    def _load(self, item):
        if self.plan is not None and self.plan.dirty:
            if not messagebox.askyesno(
                    "有未保存的改动",
                    "「%s」还有未保存的改动，切换到别的表会丢掉。继续吗？"
                    % self.plan.name, parent=self):
                return False
        try:
            self.plan = E.load_plan(item["path"])
        except E.PlanError as e:
            messagebox.showerror("打不开", str(e), parent=self)
            return False
        self.head_var.set("%s 表 · %s · %d 个频点\n%s"
                          % (E.KIND_LABEL[self.plan.kind], self.plan.name,
                             len(self.plan), item["path"]))
        self._fill_tree()
        self._update_state()
        return True

    def _fill_tree(self):
        self.tree.delete(*self.tree.get_children())
        if self.plan is None:
            return
        for i, ch in enumerate(self.plan.channels()):
            self.tree.insert("", "end", iid=str(i), values=(
                i + 1,
                ch.name,
                "%.3f" % (ch.vision_freq or 0),
                STD_LABEL.get(ch.standard, ch.standard or "—"),
                "" if ch.symbol_rate is None else ("%g" % ch.symbol_rate),
                ("%s/%s" % (ch.sat_band, ch.polarization)
                 if ch.sat_band else ""),
            ))

    def _cur_idx(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def _update_state(self):
        dirty = self.plan is not None and self.plan.dirty
        self.dirty_var.set("● 有未保存的改动（%d 个频点）"
                           % len(self.plan) if dirty else "")
        # 保存只在"真的有改动"时可点 —— 免得手滑把整个 CH 目录又备份一份
        self.save_btn.configure(state="normal" if dirty else "disabled")

    def _say(self, msg):
        self.status_var.set(msg)

    # ------------------------------------------------------------ 表的增删改
    def new_plan(self):
        dlg = _PlanNameDialog(self, "新建频道表", need_kind=True)
        if not dlg.result:
            return
        kind, name, lnb = dlg.result
        try:
            p = E.new_plan(self.source, kind, name,
                           lnb=lnb if kind == E.KIND_SAT else None)
            p.save()
        except E.PlanError as e:
            messagebox.showerror("新建失败", str(e), parent=self)
            return
        self.refresh_lists(keep=(kind, p.name))
        self._say("已新建 %s 表：%s" % (E.KIND_LABEL[kind], p.name))
        self._notify()

    def rename_plan(self):
        it = self._sel()
        if not it:
            return messagebox.showinfo("提示", "先选一张表", parent=self)
        new = _ask_text(self, "重命名频道表", "新表名：", it["name"])
        if not new or new == it["name"]:
            return
        try:
            B.backup_plans(self.source, tag="pre-rename", reason="重命名前")
            newp = E.rename_plan(it["path"], new)
        except E.PlanError as e:
            messagebox.showerror("重命名失败", str(e), parent=self)
            return
        self.refresh_lists(keep=(it["kind"], os.path.splitext(
            os.path.basename(newp))[0]))
        self.plan = None
        self.tree.delete(*self.tree.get_children())
        self._say("已重命名为 %s" % os.path.basename(newp))
        self._notify()

    def copy_plan(self):
        it = self._sel()
        if not it:
            return messagebox.showinfo("提示", "先选一张表", parent=self)
        new = _ask_text(self, "复制频道表", "新表名：", it["name"] + "_副本")
        if not new:
            return
        try:
            E.copy_plan(it["path"], new)
        except E.PlanError as e:
            messagebox.showerror("复制失败", str(e), parent=self)
            return
        self.refresh_lists(keep=(it["kind"], new))
        self._say("已复制为 %s" % new)
        self._notify()

    def delete_plan(self):
        it = self._sel()
        if not it:
            return messagebox.showinfo("提示", "先选一张表", parent=self)
        if not messagebox.askyesno(
                "删除频道表",
                "确定删除「%s」（%d 个频点）？\n\n文件：%s\n删除前会自动备份一份。"
                % (it["name"], it["count"], it["path"]), parent=self):
            return
        try:
            bk = B.backup_plans(self.source, tag="pre-delete", reason="删除前")
            E.delete_plan(it["path"])
        except (E.PlanError, OSError) as e:
            messagebox.showerror("删除失败", str(e), parent=self)
            return
        self.plan = None
        self.tree.delete(*self.tree.get_children())
        self.head_var.set("（左侧选一张表）")
        self.refresh_lists()
        self._update_state()
        self._say("已删除「%s」（备份：%s）"
                  % (it["name"], os.path.basename(bk) if bk else "无"))
        self._notify()

    # ------------------------------------------------------------ 频点
    def add_channel(self):
        if not self.plan:
            return messagebox.showinfo("提示", "先选一张表", parent=self)
        dlg = _ChannelDialog(self, self.plan)
        if not dlg.result:
            return
        try:
            ch = self.plan.add_channel(**dlg.result)
        except E.PlanError as e:
            messagebox.showerror("新增失败", str(e), parent=self)
            return
        self._fill_tree()
        self._select_row(len(self.plan) - 1)
        self._update_state()
        self._say("已新增频点 %s" % ch.name)

    def edit_channel(self):
        i = self._cur_idx()
        if i is None or not self.plan:
            return messagebox.showinfo("提示", "先在右侧选一个频点", parent=self)
        ch = self.plan.channel(i)
        dlg = _ChannelDialog(self, self.plan, channel=ch)
        if not dlg.result:
            return
        try:
            self.plan.update_channel(i, **dlg.result)
        except E.PlanError as e:
            messagebox.showerror("修改失败", str(e), parent=self)
            return
        self._fill_tree()
        self._select_row(i)
        self._update_state()
        self._say("已修改频点 %s" % self.plan.channel(i).name)

    def del_channel(self):
        i = self._cur_idx()
        if i is None or not self.plan:
            return messagebox.showinfo("提示", "先在右侧选一个频点", parent=self)
        ch = self.plan.channel(i)
        if not messagebox.askyesno("删除频点",
                                   "删除频点「%s」？" % ch.name, parent=self):
            return
        self.plan.delete_channel(i)
        self._fill_tree()
        self._update_state()
        self._say("已删除频点 %s（保存后生效）" % ch.name)

    def move_channel(self, delta):
        i = self._cur_idx()
        if i is None or not self.plan:
            return
        j = self.plan.move_channel(i, delta)
        self._fill_tree()
        self._select_row(j)
        self._update_state()

    def sort_channels(self):
        if not self.plan:
            return
        if not messagebox.askyesno("按频率排序",
                                   "把频点按频率从小到大重排？（只改顺序）",
                                   parent=self):
            return
        self.plan.sort_channels("freq")
        self._fill_tree()
        self._update_state()
        self._say("已按频率排序")

    def _select_row(self, i):
        iid = str(i)
        if self.tree.exists(iid):
            self.tree.selection_set(iid)
            self.tree.see(iid)

    # ------------------------------------------------------------ 保存
    def save_plan(self):
        if not self.plan or not self.plan.dirty:
            return
        try:
            bk = B.backup_plans(self.source, tag="pre-save", reason="保存前自动备份")
            path = self.plan.save()
        except (E.PlanError, OSError) as e:
            messagebox.showerror("保存失败", str(e), parent=self)
            return
        self.refresh_lists(keep=(self.plan.kind, self.plan.name))
        self._fill_tree()
        self._update_state()
        self._say("已保存 %s（备份：%s）"
                  % (os.path.basename(path),
                     os.path.basename(bk) if bk else "无"))
        self._notify()

    def save_as(self):
        if not self.plan:
            return messagebox.showinfo("提示", "先选一张表", parent=self)
        new = _ask_text(self, "另存为", "新表名：",
                        self.plan.name + "_副本")
        if not new:
            return
        try:
            B.backup_plans(self.source, tag="pre-saveas", reason="另存前")
            path = self.plan.save_as(new)
        except (E.PlanError, OSError) as e:
            messagebox.showerror("另存失败", str(e), parent=self)
            return
        # save_as 之后 self.plan 已指向**新文件**（旧文件原样留着），
        # 所以不用重新载入，只要把列表刷新并把选中挪到新表上。
        kind = self.plan.kind
        self.refresh_lists(keep=(kind,
                                 os.path.splitext(os.path.basename(path))[0]))
        self._fill_tree()
        self._update_state()
        self._say("已另存为 %s（原表保留不动）" % os.path.basename(path))
        self._notify()

    def reload_plan(self):
        it = self._sel()
        if not it:
            return
        if self.plan is not None and self.plan.dirty:
            if not messagebox.askyesno("放弃改动", "确实丢弃未保存的改动？",
                                       parent=self):
                return
        self._load(it)

    # ------------------------------------------------------------ 备份
    def backup_all(self):
        try:
            path = B.backup_plans(self.source, tag="manual", reason="手动备份")
        except OSError as e:
            return messagebox.showerror("备份失败", str(e), parent=self)
        if not path:
            return messagebox.showinfo("备份", "目录里没有可备份的频道表", parent=self)
        self._say("已备份到 %s（%d KB）"
                  % (path, os.path.getsize(path) // 1024))
        messagebox.showinfo("备份完成",
                            "已打包 %d 张表：\n%s"
                            % (len(E.list_plans(self.source)),
                               path), parent=self)

    def restore_backup(self):
        d = B.backup_dir()
        items = B.list_backups()
        if not items:
            return messagebox.showinfo("还原", "还没有任何备份（%s）" % d, parent=self)
        dlg = _PickDialog(self, "选择要还原的备份",
                          ["%s   %d KB" % (os.path.basename(p),
                                           os.path.getsize(p) // 1024)
                           for p in items])
        if dlg.index is None:
            return
        path = items[dlg.index]
        if not messagebox.askyesno(
                "还原备份",
                "把备份里的表覆盖回 %s？\n\n%s\n\n"
                "（同名表会被覆盖；还原前会自动再备份一次当前状态）"
                % (self.source, os.path.basename(path)), parent=self):
            return
        try:
            res = B.restore_plans(path, self.source, overwrite=True)
        except (E.PlanError, OSError) as e:
            return messagebox.showerror("还原失败", str(e), parent=self)
        self.plan = None
        self.tree.delete(*self.tree.get_children())
        self.head_var.set("（左侧选一张表）")
        self.refresh_lists()
        self._update_state()
        self._say("已还原 %d 张表（还原前备份：%s）"
                  % (len(res["written"]),
                     os.path.basename(res["backup"]) if res["backup"] else "无"))
        self._notify()

    # ------------------------------------------------------------ 设备同步
    def _need_sync(self):
        if not self.sync_factory:
            messagebox.showinfo("同步不可用",
                                "当前没有配置仪器连接（顶栏 IP / 端口）",
                                parent=self)
            return None
        try:
            return self.sync_factory()
        except Exception as e:          # 没填 IP / 端口不是数字 之类
            messagebox.showerror("同步不可用", str(e), parent=self)
            return None

    def _run_bg(self, title, fn, done):
        """后台跑 fn(sync)，完成后在**主线程**调 done(result) / 报错。

        🔴 跨线程纪律：worker 只往队列里塞结果，绝不在子线程碰 Tk
           （连 `self.after(...)` 都不行 —— Tcl 解释器不在 mainloop 里时
           会抛 `RuntimeError: main thread is not in main loop`，
           任务就静默丢失、按钮永久卡在"忙"）。
           结果由主线程的 `_poll_bg` 定时取出，和 TestEngine 的 out_q 一个套路。
        """
        if self._busy:
            return messagebox.showinfo("忙", "上一个同步任务还没结束", parent=self)
        sync = self._need_sync()
        if sync is None:
            return
        self._busy = True
        self._bg_done_fn = done
        self.sync_var.set("%s…" % title)
        self.status_var.set("%s…" % title)

        def worker():
            try:
                sync.connect()
                res = fn(sync)
                err = None
            except Exception as e:      # 全兜住，别让线程静默死掉
                res, err = None, e
            finally:
                try:
                    sync.close()
                except Exception:
                    pass
            self._q.put((title, res, err))

        threading.Thread(target=worker, daemon=True).start()
        self._poll_bg()

    def _poll_bg(self):
        """主线程轮询后台结果（只在忙的时候跑）"""
        self._bg_after = None
        try:
            title, res, err = self._q.get_nowait()
        except queue.Empty:
            if self._busy:
                self._bg_after = self.after(120, self._poll_bg)
            return
        self._bg_finish(title, res, err)

    def _bg_finish(self, title, res, err):
        self._busy = False
        self.sync_var.set("")
        done, self._bg_done_fn = self._bg_done_fn, None
        if err is not None:
            self.status_var.set("%s失败：%s" % (title, err))
            messagebox.showerror("%s失败" % title, str(err), parent=self)
            return
        self.status_var.set("%s完成" % title)
        if done:
            done(res)

    def show_device_plans(self):
        def job(sync):
            info = sync.device_info()
            out = {"info": info, "plans": {}}
            for kind in E.KINDS:
                out["plans"][kind] = sync.device_plans(kind)
            return out

        def done(res):
            lines = ["当前安装：%s（表 %d 张）"
                     % (res["info"].get("name") or "?", 
                        res["info"].get("TER", 0) + res["info"].get("SAT", 0))]
            for kind in E.KINDS:
                names = res["plans"][kind]
                lines.append("")
                lines.append("【%s】%d 张" % (E.KIND_LABEL[kind], len(names)))
                lines.append("  " + ("、".join(names) if names else "（空）"))
            messagebox.showinfo("设备上的频道表", "\n".join(lines), parent=self)

        self._run_bg("读取设备表清单", job, done)

    def download_from_device(self):
        """先取设备表清单 → 选一张 → 下载到对应文件夹（同名先问）"""
        def job(sync):
            out = {"plans": {}}
            for kind in E.KINDS:
                out["plans"][kind] = sync.device_plans(kind)
            out["info"] = sync.device_info()
            return out

        def done(res):
            # 拉平成一个"设备表清单"，同时记住每项属于哪个频段
            flat = [(kind, n) for kind in E.KINDS for n in res["plans"][kind]]
            if not flat:
                return messagebox.showinfo("下载", "设备上没有频道表", parent=self)
            dlg = _PickDialog(self, "选择要下载的频道表",
                              ["%s · %s" % (E.KIND_LABEL[k], n)
                               for k, n in flat])
            if dlg.index is None:
                return
            self._download_one(*flat[dlg.index])

        self._run_bg("读取设备表清单", job, done)

    def _download_one(self, kind, name):
        dest = E.plan_path(self.source, kind, name)
        overwrite = True
        if os.path.exists(dest):
            ans = messagebox.askyesnocancel(
                "本地已有同名表",
                "本地已存在「%s」。\n\n是 = 覆盖本地这张；否 = 另存一个新名字；"
                "取消 = 不动" % os.path.basename(dest), parent=self)
            if ans is None:
                return
            if not ans:
                # 默认给个不冲突的新名（xxx_2），用户可以改
                exists = set(it["name"] for it in self.plans[kind])
                new = _ask_text(self, "另存为新表", "新表名：",
                                E.suggest_name(name, exists))
                if not new:
                    return
                dest = E.plan_path(self.source, kind, new)

        def job(sync):
            return sync.download(kind, name, dest, overwrite=overwrite)

        def done(path):
            self.refresh_lists(keep=(kind, os.path.splitext(
                os.path.basename(path))[0]))
            self._say("已下载 %s" % os.path.basename(path))
            self._notify()
            if messagebox.askyesno("下载完成",
                                   "已下载到：\n%s\n\n现在打开编辑吗？" % path,
                                   parent=self):
                item = self._sel(kind)
                if item:
                    self._load(item)

        self._run_bg("下载 %s" % name, job, done)

    def upload_current(self):
        if not self.plan:
            return messagebox.showinfo("提示", "先选一张表", parent=self)
        if self.plan.dirty:
            if not messagebox.askyesno("有未保存的改动",
                                       "先保存再上传吗？", parent=self):
                return
            self.save_plan()
            if self.plan is None or self.plan.dirty:
                return          # 保存失败/被取消 → 不传半成品上去
        path = self.plan.path
        kind, name = self.plan.kind, self.plan.name

        def job(sync):
            # 🔴 上传前先看设备上有没有同名表（覆盖不可撤销）
            existing = sync.device_plans(kind)
            return {"existing": existing}

        def done(res):
            existing = res["existing"]
            target = name
            if name in existing:
                ans = messagebox.askyesnocancel(
                    "设备上已有同名表",
                    "设备上已存在「%s」，覆盖后不可撤销（设备没有回滚命令）。\n\n"
                    "是 = 换个名字上传（推荐）；否 = 覆盖设备上的那张；"
                    "取消 = 不上传" % name, parent=self)
                if ans is None:
                    return
                if ans:
                    new = _ask_text(self, "换个名字上传", "设备上的表名：",
                                    E.suggest_name(name, set(existing)))
                    if not new:
                        return
                    target = new

            def job2(sync):
                return sync.upload(path, kind=kind, name=target)

            def done2(got):
                self._say("已上传到设备：%s · %s" % (E.KIND_LABEL[kind], got))
                messagebox.showinfo("上传完成",
                                    "已上传到设备：%s · %s"
                                    % (E.KIND_LABEL[kind], got), parent=self)

            self._run_bg("上传 %s" % target, job2, done2)

        self._run_bg("检查设备上的同名表", job, done)

    # ------------------------------------------------------------ 收尾
    def _notify(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    def _on_close(self):
        if self._busy:
            if not messagebox.askyesno(
                    "还在同步", "同步任务还没结束，现在关窗会中断它。确定关闭？",
                    parent=self):
                return
        if self._bg_after is not None:
            try:
                self.after_cancel(self._bg_after)
            except Exception:
                pass
            self._bg_after = None
        if self.plan is not None and self.plan.dirty:
            ans = messagebox.askyesnocancel(
                "未保存的改动", "「%s」有未保存的改动，关闭前保存吗？"
                % self.plan.name, parent=self)
            if ans is None:
                return
            if ans:
                self.save_plan()
                if self.plan is not None and self.plan.dirty:
                    return
        self.destroy()


# ---------------------------------------------------------------- 小对话框
def _ask_text(parent, title, prompt, initial=""):
    dlg = tk.Toplevel(parent)
    dlg.title(title)
    dlg.transient(parent)
    dlg.grab_set()
    dlg.geometry("420x130")
    ttk.Label(dlg, text=prompt).pack(anchor="w", padx=14, pady=(14, 4))
    var = tk.StringVar(value=initial)
    ent = ttk.Entry(dlg, textvariable=var, width=44)
    ent.pack(padx=14)
    ent.focus_set()
    ent.select_range(0, "end")
    res = {"v": None}

    def ok(*_):
        res["v"] = var.get().strip()
        dlg.destroy()

    bf = ttk.Frame(dlg)
    bf.pack(fill="x", padx=14, pady=10)
    ttk.Button(bf, text="确定", command=ok).pack(side="right")
    ttk.Button(bf, text="取消", command=dlg.destroy).pack(side="right", padx=6)
    ent.bind("<Return>", ok)
    parent.wait_window(dlg)
    return res["v"]


class _PlanNameDialog(object):
    """新建表：名字 + 类型（+ 卫星本振）"""

    def __init__(self, parent, title, need_kind=True):
        self.result = None
        win = tk.Toplevel(parent)
        win.title(title)
        win.transient(parent)
        win.grab_set()
        win.geometry("460x230")
        self.win = win
        self.name_var = tk.StringVar()
        self.kind_var = tk.StringVar(value=E.KIND_TER)
        self.lnb_var = tk.StringVar(value="5150")

        ttk.Label(win, text="表名：").pack(anchor="w", padx=16, pady=(16, 2))
        ent = ttk.Entry(win, textvariable=self.name_var, width=40)
        ent.pack(padx=16)
        ent.focus_set()

        row = ttk.Frame(win)
        row.pack(anchor="w", padx=16, pady=(10, 0))
        ttk.Label(row, text="类型：").pack(side="left")
        ttk.Radiobutton(row, text="射频（TER）", value=E.KIND_TER,
                        variable=self.kind_var,
                        command=self._sync).pack(side="left")
        ttk.Radiobutton(row, text="卫星（SAT）", value=E.KIND_SAT,
                        variable=self.kind_var,
                        command=self._sync).pack(side="left", padx=8)

        self.lnb_row = ttk.Frame(win)
        self.lnb_row.pack(anchor="w", padx=16, pady=(8, 0))
        ttk.Label(self.lnb_row, text="本振 (MHz)：").pack(side="left")
        ttk.Entry(self.lnb_row, textvariable=self.lnb_var, width=10
                  ).pack(side="left")
        ttk.Label(self.lnb_row, text="C 波段 5150 / Ku 11300",
                  foreground="#777").pack(side="left", padx=6)

        bf = ttk.Frame(win)
        bf.pack(fill="x", padx=16, pady=12, side="bottom")
        ttk.Button(bf, text="创建", command=self.ok).pack(side="right")
        ttk.Button(bf, text="取消", command=win.destroy).pack(side="right", padx=6)
        ent.bind("<Return>", lambda e: self.ok())
        self._sync()
        parent.wait_window(win)

    def _sync(self):
        sat = self.kind_var.get() == E.KIND_SAT
        for w in self.lnb_row.winfo_children():
            w.configure(state="normal" if sat else "disabled")

    def ok(self):
        name = self.name_var.get().strip()
        if not name:
            return messagebox.showwarning("提示", "表名不能为空", parent=self.win)
        try:
            lnb = float(self.lnb_var.get())
        except ValueError:
            lnb = None
        self.result = (self.kind_var.get(), name, lnb)
        self.win.destroy()


class _ChannelDialog(object):
    """新增/编辑一个频点"""

    def __init__(self, parent, plan, channel=None):
        self.plan = plan
        self.result = None
        self.ch = channel
        win = tk.Toplevel(parent)
        self.win = win
        win.title("编辑频点" if channel else "新增频点")
        win.transient(parent)
        win.grab_set()
        win.geometry("460x330")

        self.name_var = tk.StringVar(value=channel.name if channel else "")
        self.freq_var = tk.StringVar(
            value=("%.3f" % channel.vision_freq) if channel else "")
        self.std_var = tk.StringVar(
            value=channel.standard if channel else E.STDS[plan.kind][0])
        sr = channel.symbol_rate if channel else None
        self.sr_var = tk.StringVar(value="" if sr is None else "%g" % sr)
        self.band_var = tk.StringVar(
            value=(channel.sat_band or "LOW") if channel else "LOW")
        self.pol_var = tk.StringVar(
            value=(channel.polarization or "OFF") if channel else "OFF")

        grid = ttk.Frame(win, padding=16)
        grid.pack(fill="both", expand=True)
        rows = [("频道名：", ttk.Entry(grid, textvariable=self.name_var, width=32)),
                ("频率 (MHz)：", ttk.Entry(grid, textvariable=self.freq_var, width=18)),
                ("制式：", ttk.Combobox(grid, textvariable=self.std_var, width=16,
                                       state="readonly",
                                       values=E.STDS[plan.kind])),
                ("符号率 (kSymb/s)：", ttk.Entry(grid, textvariable=self.sr_var,
                                                width=18))]
        for i, (label, widget) in enumerate(rows):
            ttk.Label(grid, text=label).grid(row=i, column=0, sticky="w", pady=4)
            widget.grid(row=i, column=1, sticky="w", pady=4)
        self.widgets = dict(rows)

        r = len(rows)
        if plan.kind == E.KIND_SAT:
            ttk.Label(grid, text="卫星频段：").grid(row=r, column=0, sticky="w", pady=4)
            ttk.Combobox(grid, textvariable=self.band_var, width=10,
                         state="readonly", values=("LOW", "HIGH")
                         ).grid(row=r, column=1, sticky="w", pady=4)
            r += 1
            ttk.Label(grid, text="极化：").grid(row=r, column=0, sticky="w", pady=4)
            ttk.Combobox(grid, textvariable=self.pol_var, width=10,
                         state="readonly",
                         values=("OFF", "H", "V", "L", "R")
                         ).grid(row=r, column=1, sticky="w", pady=4)
        hint = ("模拟频道请填**图像载频**（仪器表里存的是它 +2.75MHz 的伴音载频）"
                if plan.kind == E.KIND_TER else
                "卫星表填**下行频率**（仪器里存 downlink，调谐时自动换算中频）")
        ttk.Label(win, text=hint, foreground="#666",
                  wraplength=420).pack(anchor="w", padx=16)

        bf = ttk.Frame(win, padding=(16, 8))
        bf.pack(fill="x", side="bottom")
        ttk.Button(bf, text="确定", command=self.ok).pack(side="right")
        ttk.Button(bf, text="取消", command=win.destroy).pack(side="right", padx=6)
        parent.wait_window(win)

    def ok(self):
        name = self.name_var.get().strip()
        try:
            freq = float(self.freq_var.get().strip())
        except ValueError:
            return messagebox.showwarning("提示", "频率必须是数字", parent=self.win)
        sr = self.sr_var.get().strip()
        try:
            sr = float(sr) if sr else None
        except ValueError:
            return messagebox.showwarning("提示", "符号率必须是数字", parent=self.win)
        self.result = dict(name=name, freq=freq, standard=self.std_var.get(),
                           symbol_rate=sr)
        if self.plan.kind == E.KIND_SAT:
            self.result["sat_band"] = self.band_var.get()
            self.result["polarization"] = self.pol_var.get()
        self.win.destroy()


class _PickDialog(object):
    """从列表里挑一项"""

    def __init__(self, parent, title, items):
        self.index = None
        win = tk.Toplevel(parent)
        self.win = win
        win.title(title)
        win.transient(parent)
        win.grab_set()
        win.geometry("460x420")
        ttk.Label(win, text=title, padding=(12, 10, 12, 4)).pack(anchor="w")
        box = ttk.Frame(win, padding=(12, 0))
        box.pack(fill="both", expand=True)
        self.lb = tk.Listbox(box, exportselection=False)
        self.lb.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.lb.yview)
        sb.pack(side="right", fill="y")
        self.lb.configure(yscrollcommand=sb.set)
        for s in items:
            self.lb.insert("end", s)
        if items:
            self.lb.selection_set(0)
        bf = ttk.Frame(win, padding=12)
        bf.pack(fill="x")
        ttk.Button(bf, text="确定", command=self.ok).pack(side="right")
        ttk.Button(bf, text="取消", command=win.destroy).pack(side="right", padx=6)
        self.lb.bind("<Double-1>", lambda e: self.ok())
        parent.wait_window(win)

    def ok(self):
        cur = self.lb.curselection()
        if cur:
            self.index = cur[0]
        self.win.destroy()
