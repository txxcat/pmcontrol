# -*- coding: utf-8 -*-
"""SFTP 路线（promax_sftp + PlanSync 的 sftp 模式）集成测试。

全部对着 _mock_sftp.py 跑，**不碰真机**。八组：

  A 正常流程     发现目录 / 列举 / 下载 / 上传 / 覆盖（带回原文）/ 删除
  B 表不存在     报错里要列出现有表，而不是干巴巴一句"失败"
  C 目录发现     安装目录不在常见位置时，靠全盘广度优先也能找到
  D 找不到       明确报"没找到"，并把看过的目录摘要带出来
  E PlanSync     transport=sftp 端到端，strategy 标 sftp，**不需要 CRC 校准**
  F auto 模式    22 端口通 → 自动选 sftp
  G 上传中途失败  远端不能留半截 .part（否则下次当成一张坏表）
  H 整装备份     SFTP 下把 CH 打成 zip，结构与 import_installation 对得上

断言口径同样是**字节完全一致** —— 仪器认的是文件内容。

用法：python _test_sftp.py
"""
import os
import shutil
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _mock_sftp as M                                   # noqa: E402
import promax_plan_edit as E                             # noqa: E402
import promax_plan_sync as S                             # noqa: E402
import promax_sftp as SF                                 # noqa: E402

OK = 0
BAD = 0
FAILS = []

DEMO = {
    "TER": {"DEMO-A": b"<plan name=\"A\"/>\r\n",
            "DEMO-B": b"<plan name=\"B\"/>\r\n"},
    "SAT": {"DEMO-S": b"<plan name=\"S\"/>\r\n"},
}


def check(name, cond, detail=""):
    global OK, BAD
    if cond:
        OK += 1
        print("  [OK]   %s" % name)
    else:
        BAD += 1
        FAILS.append("%s   %s" % (name, detail))
        print("  [FAIL] %s   %s" % (name, detail))


def quiet(_m):
    pass


# ---------------------------------------------------------------- A
def case_a():
    print("\n== A 正常流程（发现 / 列举 / 下载 / 上传 / 覆盖 / 删除） ==")
    root = tempfile.mkdtemp(prefix="sf_a_")
    work = tempfile.mkdtemp(prefix="sf_a_w_")
    M.build_tree(root, DEMO)
    srv = M.MockSFTP(root)
    port = srv.start()
    c = SF.PlanSFTP("127.0.0.1", port=port, log=quiet)
    try:
        c.connect()
        ch = c.discover()
        check("发现 TER/SAT 目录",
              set(ch) >= {"TER", "SAT"} and "/CH" in ch["TER"], ch)
        check("安装目录名对得上", c.root.endswith("INSTAL-1"), c.root)
        check("TER 两张表", c.device_plans("TER") == ["DEMO-A", "DEMO-B"],
              c.device_plans("TER"))
        check("SAT 一张表", c.device_plans("SAT") == ["DEMO-S"],
              c.device_plans("SAT"))
        info = c.device_info()
        check("设备概况计数", (info["TER"], info["SAT"]) == (2, 1), info)

        dest = os.path.join(work, "CH", "TER", "DEMO-A.XML")
        c.download("TER", "DEMO-A", dest)
        got = open(dest, "rb").read()
        check("下载内容逐字节一致", got == DEMO["TER"]["DEMO-A"], repr(got))

        # 上传一张新表
        newp = os.path.join(work, "CH", "TER", "NEW-ONE.XML")
        os.makedirs(os.path.dirname(newp), exist_ok=True)
        blob = b"<plan name=\"NEW\"/>\r\n"
        open(newp, "wb").write(blob)
        r = c.upload(newp)
        check("上传返回表名", str(r) == "NEW-ONE", repr(r))
        check("上传后能列出来", "NEW-ONE" in c.device_plans("TER"),
              c.device_plans("TER"))
        on_dev = os.path.join(root, "USR", "VAR", "INSTALACIONES", "INSTAL-1",
                              "CH", "TER", "NEW-ONE.XML")
        check("仪器上的字节一致", open(on_dev, "rb").read() == blob)

        # 覆盖：要把原来的内容带回来，调用方才有机会留备份
        blob2 = b"<plan name=\"NEW2\"/>\r\n"
        open(newp, "wb").write(blob2)
        r2 = c.upload(newp)
        check("覆盖时带回原内容", r2.backup == blob, repr(getattr(r2, "backup", None)))
        check("覆盖后是新内容", open(on_dev, "rb").read() == blob2)
        check("上传结果仍是字符串（老代码不受影响）", r2 == "NEW-ONE", repr(r2))

        c.remove("TER", "NEW-ONE")
        check("删除生效", "NEW-ONE" not in c.device_plans("TER"),
              c.device_plans("TER"))
        check("没有留下 .part",
              not [n for n in os.listdir(os.path.dirname(on_dev))
                   if n.endswith(".part")])
    finally:
        c.close()
        srv.stop()
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- B
def case_b():
    print("\n== B 表不存在：报错要说人话 ==")
    root = tempfile.mkdtemp(prefix="sf_b_")
    M.build_tree(root, DEMO)
    srv = M.MockSFTP(root)
    port = srv.start()
    c = SF.PlanSFTP("127.0.0.1", port=port, log=quiet)
    try:
        c.connect()
        try:
            c.download("TER", "NEVER-EXIST",
                       os.path.join(tempfile.mkdtemp(), "x.XML"))
            check("不存在的表应报错", False, "居然下载成功")
        except SF.SftpError as e:
            msg = str(e)
            check("报错点明哪张表", "NEVER-EXIST" in msg, msg)
            check("报错列出现有表", "DEMO-A" in msg, msg)
    finally:
        c.close()
        srv.stop()
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------- C
def case_c():
    print("\n== C 目录发现：安装目录在不常见位置 ==")
    root = tempfile.mkdtemp(prefix="sf_c_")
    base = os.path.join(root, "OPT", "PLANS", "INSTAL-9", "CH")
    for kind, plans in DEMO.items():
        d = os.path.join(base, kind)
        os.makedirs(d, exist_ok=True)
        for name, blob in plans.items():
            open(os.path.join(d, name + ".XML"), "wb").write(blob)
    srv = M.MockSFTP(root)
    port = srv.start()
    c = SF.PlanSFTP("127.0.0.1", port=port, log=quiet)
    try:
        c.connect()
        ch = c.discover()
        check("冷门位置也能找到", "/CH" in ch.get("TER", ""), ch)
        check("认对了安装目录", c.root.endswith("INSTAL-9"), c.root)
        check("照样能列表", c.device_plans("TER") == ["DEMO-A", "DEMO-B"],
              c.device_plans("TER"))
    finally:
        c.close()
        srv.stop()
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------- D
def case_d():
    print("\n== D 找不到 CH 目录：说清楚看过哪儿 ==")
    root = tempfile.mkdtemp(prefix="sf_d_")
    os.makedirs(os.path.join(root, "USR", "VAR"), exist_ok=True)
    srv = M.MockSFTP(root)
    port = srv.start()
    c = SF.PlanSFTP("127.0.0.1", port=port, log=quiet)
    try:
        c.connect()
        try:
            c.discover()
            check("没有 CH 目录应报错", False, "居然找到了")
        except SF.SftpError as e:
            msg = str(e)
            check("明说没找到频道表目录", "没找到频道表目录" in msg, msg)
            check("带上目录摘要便于判断", "/USR" in msg or "USR" in msg, msg)
    finally:
        c.close()
        srv.stop()
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------- E
def case_e():
    print("\n== E PlanSync(sftp)：端到端，且不需要 CRC 校准 ==")
    root = tempfile.mkdtemp(prefix="sf_e_")
    src = tempfile.mkdtemp(prefix="sf_e_src_")
    M.build_tree(root, DEMO)
    srv = M.MockSFTP(root)
    port = srv.start()
    sync = S.PlanSync("127.0.0.1", port=2222, source=src, log=quiet,
                      transport="sftp", sftp_port=port, timeout=10.0)
    try:
        sync.connect()
        check("模式是 sftp", sync.mode == "sftp", sync.mode)
        check("不需要 CRC 校准", sync.crc_ready is True)
        names = sync.device_plans("TER")
        check("列举正确", names == ["DEMO-A", "DEMO-B"], names)
        check("strategy 标 sftp", sync.strategy == "sftp", sync.strategy)

        got = sync.download("TER", "DEMO-A")
        check("落盘到 source/TER", os.path.isfile(got), got)
        check("路径归属 TER",
              os.path.basename(os.path.dirname(got)) == "TER", got)
        check("内容一致", open(got, "rb").read() == DEMO["TER"]["DEMO-A"])

        up = os.path.join(src, "TER", "UP-TEST.XML")
        open(up, "wb").write(b"<plan name=\"UP\"/>\r\n")
        check("上传返回表名", str(sync.upload(up)) == "UP-TEST")
        check("上传后设备上有了", "UP-TEST" in sync.device_plans("TER"))

        sync.remove("TER", "UP-TEST")
        check("删除后没了", "UP-TEST" not in sync.device_plans("TER"))

        # 整装备份：SFTP 下把 CH 打成 zip
        zp = os.path.join(src, "inst.zip")
        sync.download_installation(zp)
        with zipfile.ZipFile(zp) as z:
            arcs = sorted(z.namelist())
        check("整装包里有 3 个表", len([a for a in arcs if a.upper().endswith(".XML")]) == 3, arcs)
        check("结构是 CH/TER|SAT",
              all(a.startswith("CH/") for a in arcs), arcs)
        # 导入回来能按 TER/SAT 分开落盘
        imp = tempfile.mkdtemp(prefix="sf_e_imp_")
        sync.source = imp
        res = sync.import_installation(zp, overwrite=True)
        check("整装包能导回本地", len(res["written"]) == 3, res)
        shutil.rmtree(imp, ignore_errors=True)
    finally:
        sync.close()
        srv.stop()
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(src, ignore_errors=True)


# ---------------------------------------------------------------- F
def case_f():
    print("\n== F auto 模式：22 通就自动选 sftp ==")
    root = tempfile.mkdtemp(prefix="sf_f_")
    M.build_tree(root, DEMO)
    srv = M.MockSFTP(root)
    port = srv.start()
    sync = S.PlanSync("127.0.0.1", port=2222, source=tempfile.mkdtemp(),
                      log=quiet, transport="auto", sftp_port=port,
                      timeout=10.0)
    try:
        check("探测到 SSH banner", sync._sftp_available() is True)
        sync.connect()
        check("自动选了 sftp", sync.mode == "sftp", sync.mode)
    finally:
        sync.close()
        srv.stop()
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------- G
def case_g():
    print("\n== G 上传中途失败：不能在仪器上留半张表 ==")
    root = tempfile.mkdtemp(prefix="sf_g_")
    work = tempfile.mkdtemp(prefix="sf_g_w_")
    M.build_tree(root, DEMO)
    srv = M.MockSFTP(root)
    port = srv.start()
    c = SF.PlanSFTP("127.0.0.1", port=port, log=quiet)
    try:
        c.connect()
        c.discover()
        p = os.path.join(work, "X.XML")
        open(p, "wb").write(b"<plan/>")

        def boom(*a, **k):
            raise IOError("链路断了")

        real_put = c.sftp.put
        c.sftp.put = boom
        try:
            c.upload(p)
            check("上传失败应抛错", False, "居然成功了")
        except SF.SftpError as e:
            check("错误里带原因", "链路断了" in str(e), str(e))
        finally:
            c.sftp.put = real_put
        ter = os.path.join(root, "USR", "VAR", "INSTALACIONES", "INSTAL-1",
                           "CH", "TER")
        check("远端没有残留 .part",
              not [n for n in os.listdir(ter) if n.endswith(".part")],
              os.listdir(ter))
        check("原表未被破坏",
              open(os.path.join(ter, "DEMO-A.XML"), "rb").read()
              == DEMO["TER"]["DEMO-A"])
    finally:
        c.close()
        srv.stop()
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- H
def case_h():
    print("\n== H 默认凭据 / 参数可覆盖 ==")
    root = tempfile.mkdtemp(prefix="sf_h_")
    M.build_tree(root, DEMO)
    srv = M.MockSFTP(root, user="Promax", password="Monitoring2019")
    port = srv.start()
    try:
        check("手册默认账号可用",
              SF.PlanSFTP("127.0.0.1", port=port, log=quiet).connect() is not None)
        bad = SF.PlanSFTP("127.0.0.1", port=port, password="wrong", log=quiet)
        try:
            bad.connect()
            check("错误密码应被拒", False, "居然连上了")
        except SF.SftpError as e:
            check("错误密码有明确提示", "SFTP 连不上" in str(e), str(e))
        finally:
            bad.close()
    finally:
        srv.stop()
        shutil.rmtree(root, ignore_errors=True)


def main():
    case_a()
    case_b()
    case_c()
    case_d()
    case_e()
    case_f()
    case_g()
    case_h()
    print("\n%s\n通过 %d / 失败 %d" % ("=" * 46, OK, BAD))
    if FAILS:
        print("失败项：")
        for f in FAILS:
            print("  - %s" % f)
    return 0 if BAD == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
