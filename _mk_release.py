# -*- coding: utf-8 -*-
"""按 v1.2.x 的既成结构重新打 v1.3.1 发布包。

结构（与 v1.2.1 一致，不可平铺 —— 平铺后程序找不到 CH/TER）：
    pmcontrol/pmcontrol.exe
    pmcontrol/使用说明.txt
    pmcontrol/CH/TER/DEMO-Analog.XML
    pmcontrol/CH/TER/DEMO-Digital.XML
    pmcontrol/CH/SAT/DEMO-C-Band.XML
    pmcontrol/CH/SAT/DEMO-Ku-Band.XML

不带 config.json：首次运行由程序自己生成（旧包也不带）。
"""
import hashlib
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")
VER = "1.3.1"
PREFIX = "pmcontrol"

ITEMS = [
    (os.path.join(DIST, "pmcontrol.exe"), "pmcontrol.exe"),
    (os.path.join(ROOT, "使用说明.txt"), "使用说明.txt"),
    (os.path.join(ROOT, "CH", "TER", "DEMO-Analog.XML"), "CH/TER/DEMO-Analog.XML"),
    (os.path.join(ROOT, "CH", "TER", "DEMO-Digital.XML"), "CH/TER/DEMO-Digital.XML"),
    (os.path.join(ROOT, "CH", "SAT", "DEMO-C-Band.XML"), "CH/SAT/DEMO-C-Band.XML"),
    (os.path.join(ROOT, "CH", "SAT", "DEMO-Ku-Band.XML"), "CH/SAT/DEMO-Ku-Band.XML"),
]

out = os.path.join(DIST, "pmcontrol-v%s-win64.zip" % VER)
if os.path.exists(out):
    os.remove(out)

missing = [src for src, _ in ITEMS if not os.path.isfile(src)]
if missing:
    print("缺文件：")
    for m in missing:
        print("  " + m)
    sys.exit(1)

with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for src, arc in ITEMS:
        z.write(src, PREFIX + "/" + arc)

size = os.path.getsize(out)
sha = hashlib.sha256(open(out, "rb").read()).hexdigest()

print("已生成 %s" % out)
print("大小   %d 字节（%.1f MB）" % (size, size / 1024.0 / 1024.0))
print("SHA256 %s" % sha)
print("")
print("包内结构：")
with zipfile.ZipFile(out) as z:
    for i in z.infolist():
        print("  %10d  %s" % (i.file_size, i.filename))
