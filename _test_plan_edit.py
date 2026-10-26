# -*- coding: utf-8 -*-
"""频道表编辑器：内核 + zip 备份 + 窗口（Tk 实测）。

三块分别是
  Part 1  promax_plan_edit     就地改 XML，未知字段必须原样留住
  Part 2  promax_plan_backup   zip 打包 / 滚动 10 份 / 还原 / 路径穿越防护
  Part 3  promax_plan_editor_gui  增删改频点 → 保存 → 自动备份 的完整链路

🔴 全程只碰**临时目录里复制出来的 DEMO-* 表**，不读也不写真实 CH/。
   （真实频道表是运营数据，一旦被测试改坏就麻烦了）

用法：python _test_plan_edit.py
     需要带 tkinter 的解释器（本机用 Python 3.12）
"""
import os
import shutil
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import promax_plan_edit as E                              # noqa: E402
import promax_plan_backup as B                             # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OK = 0
BAD = 0
FAILS = []


def check(name, cond, detail=""):
    global OK, BAD
    if cond:
        OK += 1
        print("  [OK]   %s" % name)
    else:
        BAD += 1
        FAILS.append("%s   %s" % (name, detail))
        print("  [FAIL] %s   %s" % (name, detail))


def demo_source():
    """把 4 张 DEMO 表复制进临时目录（TER/ + SAT/），返回根目录"""
    tmp = tempfile.mkdtemp(prefix="pmcontorl_edit_")
    for band in ("TER", "SAT"):
        src = os.path.join(HERE, "CH", band)
        dst = os.path.join(tmp, band)
        os.makedirs(dst, exist_ok=True)
        if not os.path.isdir(src):
            continue
        for fn in os.listdir(src):
            if fn.upper().startswith("DEMO-") and fn.upper().endswith(".XML"):
                shutil.copy2(os.path.join(src, fn), os.path.join(dst, fn))
    return tmp


def pcount(src, kind):
    return len([1 for it in E.list_plans(src) if it["kind"] == kind])


# ================================================================ Part 1
def part1():
    print("\n== Part 1 编辑器内核（promax_plan_edit）==")
    src = demo_source()
    ter = os.path.join(src, "TER", "DEMO-Analog.XML")
    dig = os.path.join(src, "TER", "DEMO-Digital.XML")
    sat = os.path.join(src, "SAT", "DEMO-C-Band.XML")

    p = E.load_plan(dig)
    check("射频表载入", p.kind == E.KIND_TER and len(p) > 0,
          "kind=%s n=%d" % (p.kind, len(p)))
    q = E.load_plan(sat)
    check("卫星表载入", q.kind == E.KIND_SAT and len(q) > 0,
          "kind=%s n=%d" % (q.kind, len(q)))

    # ---- 频率属性名按频段区分 ----
    ch = p.channel(0)
    check("射频写 frequency", ch.freq_attr == "frequency", ch.freq_attr)
    check("卫星写 downlink",
          q.channel(0).freq_attr == "downlink", q.channel(0).freq_attr)
    has_down = any("downlink" in el.attrib for el in p.root.iter("CHANNEL"))
    has_freq = any("frequency" in el.attrib for el in q.root.iter("CHANNEL"))
    check("射频表里没有 downlink", not has_down, "混写了")
    check("卫星表里没有 frequency", not has_freq, "混写了")

    # ---- 未知字段必须原样留住 ----
    before_root = dict(q.root.attrib)
    before_tags = sorted(e.tag for e in q.root.iter()
                         if e.tag not in ("CHANNEL", "CHANNELS"))
    before_ch = [dict(el.attrib) for el in list(q.root.iter("CHANNEL"))[:3]]
    q.save()                                    # 原地存一遍
    rt = ET.parse(sat).getroot()
    after_root = dict(rt.attrib)
    after_tags = sorted(e.tag for e in rt.iter()
                        if e.tag not in ("CHANNEL", "CHANNELS"))
    after_ch = [dict(el.attrib) for el in list(rt.iter("CHANNEL"))[:3]]
    check("保存后根属性不变（除 date）",
          set(before_root) == set(after_root),
          "%s vs %s" % (sorted(before_root), sorted(after_root)))
    check("保存后未知标签全在", before_tags == after_tags,
          "少：%s" % sorted(set(before_tags) - set(after_tags)))
    check("保存后频点属性不变（除 date）", before_ch == after_ch,
          "%s vs %s" % (before_ch[:1], after_ch[:1]))
    lnb = rt.find("HEADER/LNB-LOC-OSC")
    check("LNB-LOC-OSC 保留", lnb is not None and len(lnb) > 0,
          ET.tostring(lnb) if lnb is not None else "None")
    if lnb is not None:
        tag = list(lnb)[0].tag
        check("本振波段标签保留（BAND_C / BAND_KUKA）",
              tag in ("BAND_C", "BAND_KUKA"), tag)
        if tag == "BAND_KUKA":
            check("BAND_KUKA 保留 high/low",
                  "high" in list(lnb)[0].attrib, "缺 high/low")

    # ---- 模拟频道 +2.75 ----
    a = E.load_plan(ter)
    analog = [c for c in a.channels() if c.standard == E.ANALOG_STD]
    check("模拟表里有 ANALOGTER 频点", len(analog) > 0, "一个都没有")
    if analog:
        c0 = analog[0]
        check("模拟频道 XML 频率 = 图像载频 + 2.75",
              abs(c0.freq - (c0.vision_freq + E.ANALOG_SOUND_OFFSET)) < 0.001,
              "xml=%.2f vision=%.2f" % (c0.freq, c0.vision_freq))
        check("图像载频不是 0", c0.vision_freq > 1, c0.vision_freq)

    # ---- 增 / 改 / 删 ----
    n0 = len(a)
    nc = a.add_channel(name="TEST-ANALOG", freq=200.0, standard=E.ANALOG_STD)
    check("新增模拟频点", len(a) == n0 + 1, "%d -> %d" % (n0, len(a)))
    check("新增时自动 +2.75 落盘", abs(nc.freq - 202.75) < 0.001, nc.freq)
    a.update_channel(n0, freq=210.0)
    check("改频率（模拟同样是图像载频）",
          abs(a.channel(n0).freq - 212.75) < 0.001, a.channel(n0).freq)
    a.update_channel(n0, standard="DVB-C", symbol_rate=6875)
    check("换制式：频率不再偏移",
          abs(a.channel(n0).freq - 210.0) < 0.001, a.channel(n0).freq)
    check("换制式后符号率写进去了",
          a.channel(n0).symbol_rate == 6875, a.channel(n0).symbol_rate)
    a.delete_channel(n0)
    check("删除频点", len(a) == n0, "%d" % len(a))

    # ---- 校验拦截 ----
    def raises(fn, *args, **kw):
        try:
            fn(*args, **kw)
        except E.PlanError:
            return True
        return False

    exist_name = a.channel(0).name
    check("重名被拦", raises(a.add_channel, exist_name, 300.0, "DVB-C"))
    check("空名被拦", raises(a.add_channel, "  ", 300.0, "DVB-C"))
    check("频率越界被拦（射频 20~1000）",
          raises(a.add_channel, "X-OVER", 5000.0, "DVB-C"))
    check("制式不认被拦", raises(a.add_channel, "X-STD", 300.0, "DVB-S"))
    check("卫星表不接受射频制式",
          raises(q.add_channel, "X-STD", 4000.0, "DVB-C"))

    # ---- 排序 / 移动 ----
    b = E.load_plan(dig)
    b.sort_channels("freq")
    fs = [c.freq for c in b.channels()]
    check("按频率排序", fs == sorted(fs), fs[:5])
    i = b.move_channel(0, 1)
    check("下移返回新下标", i == 1, i)
    check("到底部再下移不动", b.move_channel(len(b) - 1, 1) == len(b) - 1)

    # ---- 新建表 ----
    np = E.new_plan(src, E.KIND_SAT, "NEW-Ku-Test", lnb=11300)
    np.add_channel("3900-H", 3900.0, "DVB-S2", symbol_rate=30000,
                   sat_band="HIGH", polarization="H")
    path = np.save()
    check("新建卫星表落盘", os.path.isfile(path), path)
    r = E.load_plan(path)
    check("回读频点数", len(r) == 1, len(r))
    check("回读卫星频段/极化",
          r.channel(0).sat_band == "HIGH" and r.channel(0).polarization == "H",
          "%s/%s" % (r.channel(0).sat_band, r.channel(0).polarization))
    check("新建卫星表带 LNB 本振",
          r.root.find("HEADER/LNB-LOC-OSC") is not None, "没有 LNB-LOC-OSC")
    kuka = r.root.find("HEADER/LNB-LOC-OSC/BAND_KUKA")
    check("11300 写成 BAND_KUKA", kuka is not None
          and kuka.get("high") == "11300", ET.tostring(kuka) if kuka is not None else "")
    check("新表 band 属性 = SATELLITE",
          r.root.get("band") == "SATELLITE", r.root.get("band"))
    check("新建同名表被拦", raises(E.new_plan, src, E.KIND_SAT, "NEW-Ku-Test"))

    # ---- 序列化格式 ----
    raw = open(path, "rb").read()
    check("落盘无 BOM", not raw.startswith(b"\xef\xbb\xbf"), raw[:6])
    check("落盘是 LF（无 CRLF）", b"\r\n" not in raw, "有 CRLF")
    check("落盘 utf-8 可解码", raw.decode("utf-8")[:20].startswith("<?xml"),
          raw[:20])
    check("自闭合标签写成 <TAG/>", b"/>" in raw, "没找到自闭合")
    check("缩进层级与仪器一致（顶层属性一行）",
          b"\n<CHANNEL-SET " in raw, raw.split(b"\n")[1][:30])

    # ---- 列表 / 改名 / 删除 ----
    names = [it["name"] for it in E.list_plans(src)]
    check("list_plans 数到两张子目录的表",
          any(it["kind"] == "TER" for it in E.list_plans(src)) and
          any(it["kind"] == "SAT" for it in E.list_plans(src)),
          names)
    check("list_plans 的频点数非 0",
          all(it["count"] > 0 for it in E.list_plans(src) if not it["error"]),
          [(it["name"], it["count"]) for it in E.list_plans(src)])
    newp = E.rename_plan(path, "NEW-Renamed")
    check("改名后文件名变了",
          os.path.basename(newp) == "NEW-Renamed.XML", newp)
    check("改名后文件内的 name 也变了",
          ET.parse(newp).getroot().get("name") == "NEW-Renamed",
          ET.parse(newp).getroot().get("name"))
    check("改名后旧文件不在了", not os.path.isfile(path), path)
    cp = E.copy_plan(newp, "NEW-Copy")
    check("复制表", os.path.isfile(cp) and len(E.load_plan(cp)) == 1)
    check("复制表后 name 跟着改",
          E.load_plan(cp).root.get("name") == "NEW-Copy")
    E.delete_plan(cp)
    check("删除表", not os.path.isfile(cp))

    # ---- suggest_name ----
    check("suggest_name 撞名加 _2",
          E.suggest_name("A", {"A", "A_2"}) == "A_3",
          E.suggest_name("A", {"A", "A_2"}))
    check("suggest_name 不撞名原样返回",
          E.suggest_name("B", {"A"}) == "B")
    shutil.rmtree(src, ignore_errors=True)


# ================================================================ Part 2
def part2():
    print("\n== Part 2 zip 备份（promax_plan_backup）==")
    src = demo_source()
    bdir = tempfile.mkdtemp(prefix="pmcontorl_bak_")

    total = len(E.list_plans(src))
    z = B.backup_plans(src, app_dir=bdir, tag="manual", reason="测试")
    check("备份生成 zip", z and os.path.isfile(z), z)
    check("备份目录是 bak/", os.path.basename(os.path.dirname(z)) == "bak",
          os.path.dirname(z))
    check("zip 里表数 = 目录里的表数",
          len([n for n in zipfile.ZipFile(z).namelist()
               if n.upper().endswith(".XML")]) == total, total)
    names = zipfile.ZipFile(z).namelist()
    check("zip 内路径是 CH/TER|SAT/xxx.XML",
          all(n == B.MANIFEST or (n.startswith("CH/") and "/" in n[3:])
              for n in names), names)
    man = B.backup_info(z)
    check("manifest 有表清单", "合计 %d 张表" % total in man, man[-60:])
    check("manifest 有触发原因", "测试" in man, man[:80])
    check("parse_stamp 能解析文件名", B.parse_stamp(z) is not None, z)

    check("list_backups 认到它", z in B.list_backups(bdir), B.list_backups(bdir))

    # ---- 滚动保留 ----
    made = [z]
    for i in range(4):
        made.append(B.backup_plans(src, app_dir=bdir, tag="r%d" % i, keep=3))
    left = B.list_backups(bdir)
    check("keep=3 只留 3 份", len(left) == 3, [os.path.basename(p) for p in left])
    check("最新那份还在", made[-1] in left, os.path.basename(made[-1]))
    check("最老那份被删", made[0] not in left, os.path.basename(made[0]))

    # ---- 还原 ----
    base = B.backup_plans(src, app_dir=bdir, tag="base", keep=10)
    victim = os.path.join(src, "TER", "DEMO-Digital.XML")
    good = open(victim, "rb").read()
    pp = E.load_plan(victim)
    pp.add_channel("SHOULD-BE-GONE", 700.0, "DVB-C")
    pp.save()
    check("改造后文件确实变了", open(victim, "rb").read() != good)

    res = B.restore_plans(base, src, overwrite=False)
    check("overwrite=False 不覆盖已有表", res["skipped"], res)
    check("没被覆盖（改动还在）",
          "SHOULD-BE-GONE" in open(victim, "rb").read().decode("utf-8"))

    res = B.restore_plans(base, src, overwrite=True)
    check("overwrite=True 覆盖成功", res["written"], res)
    check("还原后内容回到备份", open(victim, "rb").read() == good)
    check("还原前自动备份了一份", res["backup"] and os.path.isfile(res["backup"]),
          res["backup"])

    # ---- 路径穿越防护 ----
    evil = os.path.join(tempfile.mkdtemp(prefix="pmcontorl_evil_"), "evil.zip")
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("CH/TER/../../escape.XML", "<x/>")
        zf.writestr("TER/plain.XML", "<x/>")
        zf.writestr("CH/TER/" + B.MANIFEST, "x")
        zf.writestr("CH/TER/OK.XML", good)
    res = B.restore_plans(evil, src, overwrite=True, backup_first=False)
    check("穿越路径被丢弃", res["written"] == ["OK.XML"], res["written"])
    escape = os.path.abspath(os.path.join(src, "..", "escape.XML"))
    check("没有文件被写到上级目录", not os.path.isfile(escape), escape)
    check("非 CH/TER 结构的也丢弃",
          not os.path.isfile(os.path.join(src, "plain.XML")))

    # ---- 空目录 ----
    empty = tempfile.mkdtemp(prefix="pmcontorl_empty_")
    check("空目录备份返回 None",
          B.backup_plans(empty, app_dir=bdir) is None)
    check("坏备份读 manifest 不炸", B.backup_info(evil) != "" or True)

    shutil.rmtree(src, ignore_errors=True)
    shutil.rmtree(bdir, ignore_errors=True)


# ================================================================ Part 3
def part3():
    print("\n== Part 3 编辑器窗口（promax_plan_editor_gui）==")
    try:
        import tkinter as tk
        import promax_plan_editor_gui as EG
    except Exception as e:
        check("载入 GUI 模块（需要带 tkinter 的 Python）", False, e)
        return

    src = demo_source()
    bdir = tempfile.mkdtemp(prefix="pmcontorl_bak_")
    B.backup_dir = lambda app_dir=None: os.path.join(bdir, B.BACKUP_DIRNAME)

    # ---- 桩：把所有模态对话框换成直接返回 ----
    class MB(object):
        asked = []
        errors = []

        @staticmethod
        def showinfo(*a, **k):
            MB.asked.append(("info", a))

        @staticmethod
        def showwarning(*a, **k):
            MB.asked.append(("warn", a))

        @staticmethod
        def showerror(*a, **k):
            MB.errors.append(a)

        @staticmethod
        def askyesno(*a, **k):
            MB.asked.append(("yesno", a))
            return True

        @staticmethod
        def askyesnocancel(*a, **k):
            MB.asked.append(("ync", a))
            return False           # 「否」= 另存新名 / 不覆盖

    EG.messagebox = MB

    # 选择框：直接选第 0 项（真对话框要 wait_window，测试里会永久阻塞）
    class PickStub(object):
        def __init__(self, parent, title, items):
            self.index = 0 if items else None
            self.win = None

    EG._PickDialog = PickStub

    root = tk.Tk()
    root.withdraw()
    win = EG.PlanEditorWindow(root, src)
    root.update_idletasks()

    check("窗口建起来了", win.winfo_exists())
    check("列出射频表", len(win.plans["TER"]) == 2, win.plans["TER"])
    check("列出卫星表", len(win.plans["SAT"]) == 2, win.plans["SAT"])
    check("列表文字带频点数",
          "(" in win.lists["TER"].get(0), win.lists["TER"].get(0))

    # ---- 载入一张表 ----
    win.lists["TER"].selection_set(0)
    win._on_pick("TER")
    root.update_idletasks()
    check("载入后有当前表", win.plan is not None)
    check("表头显示表名与频点数",
          win.plan.name in win.head_var.get(), win.head_var.get())
    rows = win.tree.get_children()
    check("表格行数 = 频点数", len(rows) == len(win.plan),
          "%d vs %d" % (len(rows), len(win.plan)))
    check("表格里有频率数字",
          "." in win.tree.set(rows[0], "freq"), win.tree.set(rows[0], "freq"))
    check("保存按钮初始不可点（没改动）",
          str(win.save_btn["state"]) == "disabled", win.save_btn["state"])

    # ---- 新增频点（桩掉对话框）----
    real_dlg = EG._ChannelDialog
    n0 = len(win.plan)

    class FakeAdd(object):
        def __init__(self, parent, plan, channel=None):
            self.result = dict(name="WIN-NEW", freq=555.0, standard="DVB-C",
                               symbol_rate=6875)

    EG._ChannelDialog = FakeAdd
    win.add_channel()
    root.update_idletasks()
    check("窗口里新增频点", len(win.plan) == n0 + 1,
          "%d -> %d" % (n0, len(win.plan)))
    check("新增后标记为已改动", win.plan.dirty)
    check("新增后保存按钮可点",
          str(win.save_btn["state"]) == "normal", win.save_btn["state"])
    check("新增后选中新行", win._cur_idx() == n0, win._cur_idx())

    # ---- 编辑那一行 ----
    class FakeEdit(object):
        def __init__(self, parent, plan, channel=None):
            self.result = dict(name="WIN-EDITED", freq=556.0,
                               standard="DVB-C", symbol_rate=6900)

    EG._ChannelDialog = FakeEdit
    win._select_row(n0)
    win.edit_channel()
    root.update_idletasks()
    check("编辑改了名字",
          win.plan.channel(n0).name == "WIN-EDITED",
          win.plan.channel(n0).name)
    check("编辑改了频率",
          abs(win.plan.channel(n0).freq - 556.0) < 0.001,
          win.plan.channel(n0).freq)

    # ---- 移动 / 排序 ----
    before = win.plan.channel(0).name
    win.plan.move_channel(0, 1)
    win._fill_tree()
    check("移动后第 0 行换人",
          win.plan.channel(0).name != before or len(win.plan) == 1,
          win.plan.channel(0).name)
    win.sort_channels()
    fs = [c.freq for c in win.plan.channels()]
    check("窗口里排序生效", fs == sorted(fs), fs[:4])

    # ---- 保存 → 自动备份 ----
    target = win.plan.path
    dirty_before = win.plan.dirty
    win.save_plan()
    root.update_idletasks()
    check("保存前是 dirty", dirty_before)
    check("保存后 dirty 清掉", not win.plan.dirty)
    check("文件真的写到磁盘", os.path.isfile(target))
    check("磁盘内容含新频点",
          b"WIN-EDITED" in open(target, "rb").read())
    bks = B.list_backups()
    check("保存前自动备份了一份", len(bks) >= 1,
          [os.path.basename(p) for p in bks])
    if bks:
        check("备份文件名带 pre-save",
              "pre-save" in os.path.basename(bks[0]),
              os.path.basename(bks[0]))
    check("保存失败没有弹错", not MB.errors, MB.errors)

    # ---- 删除频点 ----
    win._select_row(0)
    n1 = len(win.plan)
    win.del_channel()
    check("删除频点", len(win.plan) == n1 - 1, len(win.plan))

    # ---- 新建表（桩掉名字对话框）----
    class FakeName(object):
        def __init__(self, parent, title, need_kind=True):
            self.result = (E.KIND_SAT, "WIN-NEW-SAT", 11300)

    real_name = EG._PlanNameDialog
    EG._PlanNameDialog = FakeName
    n_sat = len(win.plans["SAT"])
    win.new_plan()
    root.update_idletasks()
    check("窗口里新建卫星表", len(win.plans["SAT"]) == n_sat + 1,
          [it["name"] for it in win.plans["SAT"]])
    check("新建的表在 SAT 目录",
          os.path.isfile(os.path.join(src, "SAT", "WIN-NEW-SAT.XML")))
    EG._PlanNameDialog = real_name

    # ---- 另存为 ----
    win.lists["TER"].selection_set(0)
    win._on_pick("TER")
    old_path = win.plan.path
    EG._ask_text = lambda *a, **k: "WIN-SAVED-AS"
    win.save_as()
    root.update_idletasks()
    check("另存为新文件",
          os.path.isfile(os.path.join(src, "TER", "WIN-SAVED-AS.XML")))
    check("另存后原表还在", os.path.isfile(old_path), old_path)
    check("另存后当前表指向新文件",
          os.path.basename(win.plan.path) == "WIN-SAVED-AS.XML", win.plan.path)

    # ---- 还原备份 ----
    win.restore_backup()
    root.update_idletasks()
    check("还原没炸", not MB.errors, MB.errors)

    # ---- 同步：用假会话走一遍流程（不联网）----
    class FakeSync(object):
        def __init__(self):
            self.closed = False
            self.uploaded = []

        def connect(self):
            return self

        def close(self):
            self.closed = True

        def device_info(self):
            return {"TER": 1, "SAT": 1, "name": "DEMO"}

        def device_plans(self, kind):
            # 故意用**本地已存在**的名字，才能测到"同名 → 另存新名"那条路
            return {"TER": ["DEMO-Digital"], "SAT": ["DEMO-C-Band"]}[kind]

        def download(self, kind, name, dest=None, overwrite=True):
            dest = dest or E.plan_path(src, kind, name)
            F = open(os.path.join(src, "TER", "DEMO-Digital.XML"), "rb").read()
            open(dest, "wb").write(F)
            return dest

        def upload(self, path, kind=None, name=None):
            self.uploaded.append((path, kind, name))
            return name

    fake = {"s": FakeSync()}
    win.sync_factory = lambda: fake["s"]
    win.show_device_plans()
    for _ in range(60):                    # 等后台线程回调排进主线程
        root.update()
        if not win._busy:
            break
        root.after(30)
    root.update()
    check("设备清单读出来了",
          any("DEMO-Digital" in str(a) for a in MB.asked), MB.asked[-3:])
    check("同步会话被关掉了", fake["s"].closed)

    # 下载（同名 → 桩返回"否" → 另存新名）
    fake["s"].closed = False
    EG._ask_text = lambda *a, **k: "DL-RENAMED"
    win.download_from_device()
    for _ in range(80):
        root.update()
        if not win._busy:
            break
        root.after(30)
    root.update()
    check("下载没报错", not MB.errors, MB.errors)
    check("下载流程走完了", not win._busy)
    check("同名时另存新名生效",
          os.path.isfile(os.path.join(src, "TER", "DL-RENAMED.XML")),
          os.listdir(os.path.join(src, "TER")))

    # 上传：设备上已有同名 → 桩返回"否" → 覆盖路径
    win.lists["TER"].selection_set(0)
    win._on_pick("TER")
    win.upload_current()
    for _ in range(80):
        root.update()
        if not win._busy:
            break
        root.after(30)
    root.update()
    check("上传被调到", len(fake["s"].uploaded) == 1,
          fake["s"].uploaded)
    if fake["s"].uploaded:
        check("上传带上了频段",
              fake["s"].uploaded[0][1] in ("TER", "SAT"),
              fake["s"].uploaded[0][1])

    # ---- 关窗 ----
    win.plan.dirty = False
    win._on_close()
    root.update()
    check("窗口关掉了", not win.winfo_exists())
    try:
        root.destroy()
    except Exception:
        pass
    EG._ChannelDialog = real_dlg
    shutil.rmtree(src, ignore_errors=True)
    shutil.rmtree(bdir, ignore_errors=True)


# ================================================================ Part 4
DEMO_CFG = {
    "window": {"width": 1240, "height": 780},
    "ip_history": ["127.0.0.1"], "last_ip": "127.0.0.1", "port": 2222,
    "out_dir": "", "out_dir_history": [], "settle": 0.15,
    "settle_ter": 0.3, "settle_sat": 3.0, "retry_ter": 3, "retry_sat": 5,
    "retry_step_ter": 0.3, "retry_step_sat": 6.0,
    "pause_between": True, "auto_reconnect": True, "reconnect_interval": 10.0,
    "export_format": "xlsx", "log_dir": "", "log_name": "promax_test.log",
    "log_timestamps": True, "log_timing": True, "log_cmd_timing": False,
    "log_separate": False,
    "presets": [{"name": "演示组", "files": ["DEMO-Digital"],
                 "filename": "演示_{date8}", "pause": False}],
}


def part4():
    print("\n== Part 4 主窗口入口（频道表管理按钮）==")
    try:
        import tkinter as tk
        import promax_plan_gui as gui
        import promax_plan_editor_gui as EG
    except Exception as e:
        check("载入主窗口模块", False, e)
        return

    gui.load_config = lambda *a, **k: dict(DEMO_CFG)
    # 关掉真正的"打开文件"（否则会拉起 WPS/Excel 把测试卡住）
    gui.App._open = staticmethod(lambda p: None)

    src = demo_source()
    bdir = tempfile.mkdtemp(prefix="pmcontorl_bak_")
    real_backup_dir = B.backup_dir
    B.backup_dir = lambda app_dir=None: os.path.join(bdir, B.BACKUP_DIRNAME)

    root = tk.Tk()
    root.withdraw()
    app = gui.App(root, src)
    for _ in range(3):
        root.update_idletasks()
        root.update()

    def find_button(w, text):
        for c in w.winfo_children():
            try:
                if c.winfo_class() == "TButton" and c.cget("text") == text:
                    return c
            except Exception:
                pass
            got = find_button(c, text)
            if got is not None:
                return got
        return None

    btn = find_button(root, "频道表管理")
    check("顶栏有「频道表管理」按钮", btn is not None, "没找到")
    check("按钮可点（未被禁用）",
          str(btn["state"]) != "disabled" if btn is not None else False)

    # ---- 打开编辑器（单例）----
    if btn is not None:
        btn.invoke()
        for _ in range(3):
            root.update_idletasks()
            root.update()
    ed = app._editor
    check("点按钮打开了编辑器", ed is not None and ed.winfo_exists())
    check("编辑器拿到的目录 = 主窗口的频道表目录",
          ed is not None and os.path.abspath(ed.source) == os.path.abspath(src),
          ed.source if ed else "")
    app.open_plan_editor()
    check("再点一次还是同一个窗口（单例）", app._editor is ed)

    # ---- 同步会话工厂用的是顶栏 IP / 端口 ----
    sync = app._make_sync()
    check("同步会话用顶栏 IP", sync.host == "127.0.0.1", sync.host)
    check("同步会话用顶栏端口", sync.port == 2222, sync.port)
    check("同步会话指向同一个频道表目录",
          os.path.abspath(sync.source) == os.path.abspath(src), sync.source)
    check("超时给足（≥20s）", sync.timeout >= 20.0, sync.timeout)

    # IP 没填时要给人话，而不是抛栈
    app.ip_var.set("")
    try:
        app._make_sync()
        check("IP 空着时给出提示", False, "没抛错")
    except Exception as e:
        check("IP 空着时给出提示", "IP" in str(e), str(e))
    app.ip_var.set("127.0.0.1")

    # ---- 改完表后重扫，勾选不丢 ----
    keys = [k for k in app.check_vars]
    check("主窗口先有频道表", len(keys) >= 2, keys)
    if keys:
        first = keys[0]
        app.check_vars[first].set(1)
        app.tree.set(first, "check", "☑")
        app._sync_sel()
        before = app.sel_lbl.get()
        app._rescan_after_edit()
        root.update_idletasks()
        check("重扫后列表还在", len(app.check_vars) >= 2, app.check_vars.keys())
        check("重扫后原来的勾选保住了",
              app.check_vars.get(first, tk.IntVar()).get() == 1,
              "before=%s first=%s" % (before, first))

    # ---- 后台日志转发不炸 ----
    app._sync_log("测试一行")
    root.update_idletasks()
    check("同步日志转发没炸", True)

    # ---- 关主窗口时连带关掉编辑器 ----
    ed.plan = None
    app._on_close()
    root.update()
    # 根窗口都销毁了，再问 winfo_exists 会抛 TclError（= 已经没了）
    try:
        alive = ed is not None and ed.winfo_exists()
    except tk.TclError:
        alive = False
    check("关主窗口后编辑器也关了", not alive)
    try:
        root.destroy()
    except Exception:
        pass
    B.backup_dir = real_backup_dir
    shutil.rmtree(src, ignore_errors=True)
    shutil.rmtree(bdir, ignore_errors=True)


def main():
    part1()
    part2()
    part3()
    part4()
    print("\n%s\n通过 %d / 失败 %d" % ("=" * 46, OK, BAD))
    if FAILS:
        print("失败项：")
        for f in FAILS:
            print("  - %s" % f)
    return 0 if BAD == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
