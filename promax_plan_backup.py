# -*- coding: utf-8 -*-
"""频道表备份 / 还原（zip 打包，滚动保留最近 N 份）。

放在哪
------
与 config.json 的滚动备份同一个目录：`<程序目录>/bak/`
（config 备份是 `bak/config.json.bak1..10`，这里放 `bak/CH-20261026-195530.zip`）

里面装什么
----------
    CH/TER/*.XML
    CH/SAT/*.XML
    manifest.txt        ← 时间、来源目录、每张表的名字/频点数/大小

🔴 三点注意
1. **备份的是真实运营数据**：这些 zip 绝不能进仓库（.gitignore 已挡 `bak/`）。
2. **只在文件能正常解析时备份**：损坏的表固化进备份等于把坏数据锁死，
   与 config 备份"只备份合法 json"是同一个道理。
3. 备份是**整体快照**（不是增量）：一张 zip 就是一个完整的 CH 目录，
   还原时要么全量覆盖，要么不覆盖 —— 避免半新半旧。
"""
import datetime
import os
import re
import zipfile

import promax_plan_edit as E

BACKUP_DIRNAME = "bak"          # 与 config.json 备份同目录
BACKUP_PREFIX = "CH-"
BACKUP_EXT = ".zip"
BACKUP_KEEP = 10                # 滚动保留份数
MANIFEST = "manifest.txt"


def backup_dir(app_dir=None):
    """备份目录：<app_dir>/bak（app_dir 默认取 config.json 所在目录）"""
    if app_dir is None:
        import promax_gui_config as C
        app_dir = os.path.dirname(os.path.abspath(C.CONFIG_NAME))
    return os.path.join(os.path.abspath(app_dir), BACKUP_DIRNAME)


def _stamp(dt=None):
    dt = dt or datetime.datetime.now()
    return dt.strftime("%Y%m%d-%H%M%S")


def list_backups(app_dir=None):
    """已有备份，按时间从新到旧"""
    d = backup_dir(app_dir)
    if not os.path.isdir(d):
        return []
    items = []
    for fn in os.listdir(d):
        if fn.startswith(BACKUP_PREFIX) and fn.lower().endswith(BACKUP_EXT):
            p = os.path.join(d, fn)
            items.append(p)
    items.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return items


def _plan_files(source):
    """[(磁盘路径, 压缩包内路径)]"""
    out = []
    for kind in E.KINDS:
        sub = E.KIND_DIRS[kind]
        d = os.path.join(source, sub)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.upper().endswith(E.PLAN_EXT):
                continue
            out.append((os.path.join(d, fn), "CH/%s/%s" % (sub, fn)))
    return out


def backup_plans(source, app_dir=None, tag="", keep=BACKUP_KEEP, reason=""):
    """把 source 下的全部频道表打成一张 zip。返回 zip 路径（没有表时返回 None）。

    tag：文件名后缀标记，如 "auto" / "manual"（只影响可读性）
    """
    files = _plan_files(source)
    if not files:
        return None
    bdir = backup_dir(app_dir)
    os.makedirs(bdir, exist_ok=True)
    stamp = _stamp()
    name = "%s%s%s%s" % (BACKUP_PREFIX, stamp,
                         ("-" + tag if tag else ""), BACKUP_EXT)
    path = os.path.join(bdir, name)
    n = 2
    while os.path.exists(path):                   # 同一秒内连点两次
        path = os.path.join(bdir, "%s%s-%d%s"
                            % (BACKUP_PREFIX, stamp,
                               n, BACKUP_EXT))
        n += 1

    lines = ["频道表备份 %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "来源目录: %s" % os.path.abspath(source)]
    if reason:
        lines.append("触发: %s" % reason)
    lines.append("")
    lines.append("%-8s %-32s %6s %10s" % ("类型", "表名", "频点", "字节"))
    total = 0
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for disk, arc in files:
            try:
                E.load_plan(disk)                 # 解析不了就不备份（见文件头第 2 条）
            except E.PlanError:
                lines.append("!! 跳过无法解析的文件: %s" % arc)
                continue
            z.write(disk, arc)
            size = os.path.getsize(disk)
            total += 1
            try:
                cnt = len(E.load_plan(disk))
            except E.PlanError:
                cnt = 0
            lines.append("%-8s %-32s %6d %10d"
                         % (E.KIND_LABEL[E.kind_of_path(disk)], arc, cnt, size))
        lines.append("")
        lines.append("合计 %d 张表" % total)
        z.writestr(MANIFEST, "\n".join(lines) + "\n")
    os.replace(tmp, path)
    _prune(bdir, keep, keep_path=path)
    return path


def _prune(bdir, keep, keep_path=None):
    """滚动删除最老的备份，只留最近 keep 份（刚建的那份永不删）"""
    if keep <= 0:
        return
    items = list_backups(os.path.dirname(bdir))
    for p in items[keep:]:
        if keep_path and os.path.abspath(p) == os.path.abspath(keep_path):
            continue
        try:
            os.remove(p)
        except OSError:
            pass


def backup_info(path):
    """读备份里的 manifest（展现在界面上用）"""
    try:
        with zipfile.ZipFile(path) as z:
            return z.read(MANIFEST).decode("utf-8", "replace")
    except (OSError, KeyError, zipfile.BadZipFile):
        return ""


def restore_plans(zip_path, source, overwrite=False, backup_first=True,
                  app_dir=None):
    """从备份还原到 source。

    overwrite=False 时，本地已存在的同名表**不覆盖**（返回 skip 列表）。
    还原前默认先给当前状态再做一份备份（backup_first），防止还原错了没法回头。
    返回 {"written": [...], "skipped": [...], "backup": path|None}
    """
    if not os.path.isfile(zip_path):
        raise E.PlanError("备份文件不存在：%s" % zip_path)
    before = backup_plans(source, app_dir=app_dir, tag="pre-restore",
                          reason="还原前自动备份") if backup_first else None
    written, skipped = [], []
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            arc = info.filename.replace("\\", "/")
            if not arc.lower().endswith(E.PLAN_EXT.lower()):
                continue
            # 只接受 CH/TER/xxx.XML 或 CH/SAT/xxx.XML，防止 zip 里带路径穿越
            parts = [p for p in arc.split("/") if p not in ("", ".")]
            if len(parts) != 3 or parts[0] != "CH" or \
                    parts[1].upper() not in ("TER", "SAT") or ".." in parts:
                continue
            target = os.path.join(source, parts[1].upper(), parts[2])
            if os.path.exists(target) and not overwrite:
                skipped.append(parts[2])
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(info) as src, open(target + ".tmp", "wb") as dst:
                dst.write(src.read())
            os.replace(target + ".tmp", target)
            written.append(parts[2])
    return {"written": written, "skipped": skipped, "backup": before}


def parse_stamp(path):
    """从备份文件名里取时间戳，取不到返回 None"""
    m = re.search(r"CH-(\d{8}-\d{6})", os.path.basename(path))
    if not m:
        return None
    try:
        return datetime.datetime.strptime(m.group(1), "%Y%m%d-%H%M%S")
    except ValueError:
        return None
