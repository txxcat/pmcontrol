# -*- coding: utf-8 -*-
"""频道表（Channel Plan）XML 的读写与增删改 —— 编辑器内核。

文件格式（仪器导出，实测）
-------------------------
    <?xml version="1.0" encoding="utf-8"?>
    <CHANNEL-SET band="TERRESTRIAL|SATELLITE" date="2026-1-1"
                 equipment="RANGERNeo 4" name="表名" trademark="..." version="2.0">
      <HEADER>
        <TV system="PAL" standard="DK"/>            <!-- 射频表 -->
        <LNB>EXTERNAL</LNB>
        <LNB-LOC-OSC><BAND_C value="5150" units="MHz"/></LNB-LOC-OSC>   <!-- 卫星表 -->
      </HEADER>
      <CHANNELS>
        <CHANNEL name="474-DS13" frequency="474.00" units="MHz">        <!-- 射频：frequency -->
          <SIGNAL><DVB-C/></SIGNAL>
        </CHANNEL>
        <CHANNEL name="3780" downlink="3780.00" units="MHz">            <!-- 卫星：downlink -->
          <SAT band="LOW" polarization="OFF"/>
          <SIGNAL><DVB-S><SYMBOLRATE value="12000" units="kSymb/s"/></DVB-S></SIGNAL>
        </CHANNEL>
      </CHANNELS>
    </CHANNEL-SET>

🔴 三条铁律
-----------
1. **就地改，不重建**。真机上导出的表里还有 `SAT@band/@polarization`、`LNB`、
   `BAND_KUKA@high/low`、`TV@system/@standard` 等字段，我们现在的解析器没用全。
   若按"解析成 dict 再重新生成 XML"来保存，这些字段会**全部丢失**，
   仪器很可能不认这张表 —— 所以本模块一律在 ElementTree 上原地增删改。
2. **频率属性名按频段区分**：射频写 `frequency`，卫星写 `downlink`；两者都带
   `units="MHz"`。卫星表**不能**写 `frequency`（仪器按 band 解析，混写会错）。
3. **模拟频道（ANALOGTER）的 XML frequency 是"图像载频 + 2.75MHz"**
   （图像/伴音载频差）。实测 `144.25-Z05` 在表里是 `frequency="147"`。
   所以对用户显示/编辑的永远是**图像载频**，落盘时自动 +2.75。

用法
----
    p = load_plan("CH/TER/xxx.XML")
    p.add_channel(name="666-DS30", freq=666.0, standard="DVB-C")
    p.update_channel(0, freq=667.0, symbol_rate=6875)
    p.delete_channel(2)
    p.save()                       # 校验 + 原子写

    new_plan(source="CH", kind="SAT", name="我的表").save()
"""
import os
import re
import shutil
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------- 常量
KIND_TER = "TER"
KIND_SAT = "SAT"
KINDS = (KIND_TER, KIND_SAT)
KIND_DIRS = {KIND_TER: "TER", KIND_SAT: "SAT"}      # 源目录下的子文件夹
KIND_BAND_ATTR = {KIND_TER: "TERRESTRIAL", KIND_SAT: "SATELLITE"}
KIND_LABEL = {KIND_TER: "射频", KIND_SAT: "卫星"}

# 制式：射频 / 卫星各一组（仪器认的标签，大小写敏感）
STD_TER = ("DVB-C", "DVB-C2", "DVB-T", "DVB-T2", "ANALOGTER", "FM", "GENERIC")
STD_SAT = ("DVB-S", "DVB-S2", "DSS")
STDS = {KIND_TER: STD_TER, KIND_SAT: STD_SAT}

# 允许带符号率的制式（模拟/FM 没有符号率）
STD_WITH_SYMB = ("DVB-C", "DVB-C2", "DVB-S", "DVB-S2", "DSS", "DVB-T", "DVB-T2")

ANALOG_STD = "ANALOGTER"
ANALOG_SOUND_OFFSET = 2.75      # 🔴 图像载频 → XML frequency 的固定偏移
FREQ_ATTR = {KIND_TER: "frequency", KIND_SAT: "downlink"}

PLAN_EXT = ".XML"
MAX_NAME_LEN = 40               # 文件名即表名，太长的名字仪器/备份都不友好

# 频率合理区间（MHz）—— 只做"离谱值"拦截，不做严格频段划分
FREQ_RANGE = {KIND_TER: (20.0, 1000.0), KIND_SAT: (900.0, 22000.0)}


class PlanError(Exception):
    """频道表读写/校验错误（用户可见的中文提示）"""


# ---------------------------------------------------------------- 小工具
def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _fmt_freq(v):
    """频率落盘格式：两位小数（与仪器导出一致，如 474.00 / 12395.00）"""
    return "%.2f" % v


def _clean_name(name):
    """表名/频道名里不能有路径分隔符（文件名即表名）"""
    s = str(name or "").strip()
    s = re.sub(r'[\\/:*?"<>|]', "_", s)
    return s


def today_attr():
    """仪器写日期用的是不补零格式：2026-1-1"""
    import datetime
    d = datetime.date.today()
    return "%d-%d-%d" % (d.year, d.month, d.day)


def plan_path(source, kind, name):
    """频道表文件路径。source = 频道表根目录（其下有 TER/ SAT/）"""
    if kind not in KINDS:
        raise PlanError("频道表类型只能是 TER 或 SAT：%r" % (kind,))
    return os.path.join(source, KIND_DIRS[kind], _clean_name(name) + PLAN_EXT)


def kind_of_path(path):
    """按所在文件夹判断是射频表还是卫星表"""
    up = os.path.basename(os.path.dirname(os.path.abspath(path))).upper()
    return KIND_SAT if up.startswith("SAT") else KIND_TER


# ---------------------------------------------------------------- 读
class PlanChannel(object):
    """单个频点（只是 `<CHANNEL>` 元素的视图，改它就是改原树）"""

    __slots__ = ("el", "kind")

    def __init__(self, el, kind):
        self.el = el
        self.kind = kind

    # -- 名字
    @property
    def name(self):
        return self.el.get("name", "")

    @name.setter
    def name(self, v):
        self.el.set("name", _clean_name(v))

    # -- 频率（XML 原值，单位 MHz）
    @property
    def freq(self):
        return _f(self.el.get(FREQ_ATTR[self.kind]))

    @freq.setter
    def freq(self, v):
        self.el.set(FREQ_ATTR[self.kind], _fmt_freq(float(v)))

    @property
    def freq_attr(self):
        return FREQ_ATTR[self.kind]

    # -- 图像载频（仅模拟有意义；其余制式与 freq 相同）
    @property
    def vision_freq(self):
        if self.standard == ANALOG_STD:
            return round(self.freq - ANALOG_SOUND_OFFSET, 2)
        return self.freq

    # -- 制式
    def _signal(self):
        return self.el.find("SIGNAL")

    def _std_el(self):
        sig = self._signal()
        if sig is None:
            return None
        for ch in list(sig):
            return ch
        return None

    @property
    def standard(self):
        el = self._std_el()
        return el.tag if el is not None else ""

    @standard.setter
    def standard(self, tag):
        tag = (tag or "").strip()
        if tag not in STDS[self.kind]:
            raise PlanError("%s 表不支持制式 %r（可选：%s）"
                            % (KIND_LABEL[self.kind], tag,
                               "/".join(STDS[self.kind])))
        sig = self._signal()
        if sig is None:
            sig = ET.SubElement(self.el, "SIGNAL")
        symb = self.symbol_rate
        for ch in list(sig):
            sig.remove(ch)
        std = ET.SubElement(sig, tag)
        if symb is not None and tag in STD_WITH_SYMB:
            ET.SubElement(std, "SYMBOLRATE",
                          {"value": _fmt_sr(symb), "units": "kSymb/s"})

    # -- 符号率
    @property
    def symbol_rate(self):
        std = self._std_el()
        if std is None:
            return None
        sr = std.find("SYMBOLRATE")
        if sr is None:
            return None
        v = _f(sr.get("value"), 0.0)
        return v or None

    @symbol_rate.setter
    def symbol_rate(self, v):
        std = self._std_el()
        if std is None:
            return
        if v in (None, "", 0):
            for sr in std.findall("SYMBOLRATE"):
                std.remove(sr)
            return
        sr = std.find("SYMBOLRATE")
        if sr is None:
            sr = ET.SubElement(std, "SYMBOLRATE",
                               {"value": _fmt_sr(v), "units": "kSymb/s"})
        else:
            sr.set("value", _fmt_sr(v))

    # -- 卫星的 <SAT band polarization/>
    def _sat_el(self):
        return self.el.find("SAT")

    @property
    def sat_band(self):
        el = self._sat_el()
        return el.get("band", "") if el is not None else ""

    @property
    def polarization(self):
        el = self._sat_el()
        return el.get("polarization", "") if el is not None else ""

    def set_sat(self, band=None, polarization=None):
        if self.kind != KIND_SAT:
            return
        el = self._sat_el()
        if el is None:
            el = ET.Element("SAT")
            # 放在 SIGNAL 之前（仪器导出的顺序）
            sig = self._signal()
            if sig is not None:
                self.el.insert(list(self.el).index(sig), el)
            else:
                self.el.append(el)
        if band:
            el.set("band", str(band).upper())
        if polarization:
            el.set("polarization", str(polarization).upper())

    # -- 原始行（导表用）
    def as_row(self):
        return {
            "name": self.name,
            "freq": self.freq,
            "vision": self.vision_freq,
            "attr": self.freq_attr,
            "standard": self.standard,
            "symbol_rate": self.symbol_rate,
            "sat_band": self.sat_band,
            "polarization": self.polarization,
        }

    def __repr__(self):
        return "<ch %s %.2f %s>" % (self.name, self.freq, self.standard)


def _fmt_sr(v):
    """符号率：整数就不带小数点（与仪器导出一致）"""
    v = float(v)
    return ("%d" % v) if abs(v - round(v)) < 0.001 else ("%.3f" % v).rstrip("0").rstrip(".")


class PlanFile(object):
    """一张频道表（一个 XML 文件）"""

    def __init__(self, path, tree, kind=None):
        self.path = os.path.abspath(path) if path else None
        self.tree = tree
        self.root = tree.getroot()
        self.kind = kind or kind_of_path(self.path or "x/TER/x.XML")
        if self.root.tag != "CHANNEL-SET":
            raise PlanError("不是频道表文件（根元素是 %s，应为 CHANNEL-SET）"
                            % self.root.tag)
        self.dirty = False

    # -- 基本信息
    @property
    def name(self):
        return self.root.get("name", "")

    @name.setter
    def name(self, v):
        v = _clean_name(v)
        if not v:
            raise PlanError("表名不能为空")
        if len(v) > MAX_NAME_LEN:
            raise PlanError("表名太长（%d > %d）" % (len(v), MAX_NAME_LEN))
        if self.root.get("name") != v:
            self.root.set("name", v)
            self.dirty = True

    def _channels_el(self, create=False):
        el = self.root.find("CHANNELS")
        if el is None and create:
            el = ET.SubElement(self.root, "CHANNELS")
        return el

    # -- 频点
    def channels(self):
        box = self._channels_el()
        if box is None:
            return []
        return [PlanChannel(ch, self.kind) for ch in box.findall("CHANNEL")]

    def __len__(self):
        return len(self.channels())

    def channel(self, idx):
        chs = self.channels()
        if not (0 <= idx < len(chs)):
            raise PlanError("频点序号超出范围：%d（共 %d 个）" % (idx, len(chs)))
        return chs[idx]

    def find_channel(self, name):
        for ch in self.channels():
            if ch.name == name:
                return ch
        return None

    def check_name(self, name, ignore_idx=None):
        """频道名不能为空 / 不能重名"""
        name = _clean_name(name)
        if not name:
            raise PlanError("频道名不能为空")
        for i, ch in enumerate(self.channels()):
            if i != ignore_idx and ch.name == name:
                raise PlanError("频道名重复：%s（第 %d 个）" % (name, i + 1))
        return name

    def check_freq(self, freq, kind=None):
        lo, hi = FREQ_RANGE[kind or self.kind]
        try:
            freq = float(freq)
        except (TypeError, ValueError):
            raise PlanError("频率必须是数字：%r" % (freq,))
        if not (lo <= freq <= hi):
            raise PlanError("频率 %.3f MHz 超出%s表的合理范围（%.0f ~ %.0f MHz）"
                            % (freq, KIND_LABEL[kind or self.kind], lo, hi))
        return freq

    def add_channel(self, name, freq, standard, symbol_rate=None,
                    sat_band="LOW", polarization="OFF", index=None):
        """新增频点。模拟制式传**图像载频**，内部自动 +2.75 落盘。"""
        name = self.check_name(name)
        std = standard or (STD_TER[0] if self.kind == KIND_TER else STD_SAT[0])
        if std not in STDS[self.kind]:
            raise PlanError("%s 表不支持制式 %r" % (KIND_LABEL[self.kind], std))
        vision = self.check_freq(freq)
        xml_freq = vision + ANALOG_SOUND_OFFSET if std == ANALOG_STD else vision
        self.check_freq(xml_freq)

        el = ET.Element("CHANNEL", {"name": name,
                                    self.freq_attr_placeholder(): _fmt_freq(xml_freq),
                                    "units": "MHz"})
        ch = PlanChannel(el, self.kind)
        if self.kind == KIND_SAT:
            ch.set_sat(sat_band, polarization)
        ET.SubElement(el, "SIGNAL")
        ch.standard = std
        if symbol_rate and std in STD_WITH_SYMB:
            ch.symbol_rate = symbol_rate

        box = self._channels_el(create=True)
        if index is None or index >= len(box):
            box.append(el)
        else:
            box.insert(index, el)
        self.dirty = True
        return ch

    def freq_attr_placeholder(self):
        return FREQ_ATTR[self.kind]

    def update_channel(self, idx, name=None, freq=None, standard=None,
                       symbol_rate=None, sat_band=None, polarization=None):
        ch = self.channel(idx)
        # 🔴 用户看到的数字（模拟 = 图像载频，数字 = 中心频率）先记下来：
        #    只改制式不改频率时，要保住这个数字不跳。否则模拟(210→存 212.75)
        #    换成 DVB-C 后会显示 212.75 —— 白白多出 2.75MHz，看着像程序算错了。
        old_vision = ch.vision_freq
        if name is not None and _clean_name(name) != ch.name:
            ch.name = self.check_name(name, ignore_idx=idx)
        if standard is not None and standard != ch.standard:
            ch.standard = standard            # 换制式（符号率会按需保留）
            if freq is None:
                freq = old_vision
        if freq is not None:
            v = self.check_freq(freq)
            if ch.standard == ANALOG_STD:
                v += ANALOG_SOUND_OFFSET
                self.check_freq(v)
            ch.freq = v
        if symbol_rate is not None:
            ch.symbol_rate = symbol_rate
        if sat_band or polarization:
            ch.set_sat(sat_band, polarization)
        self.dirty = True
        return ch

    def delete_channel(self, idx):
        ch = self.channel(idx)
        box = self._channels_el()
        box.remove(ch.el)
        self.dirty = True

    def move_channel(self, idx, delta):
        chs = self.channels()
        j = idx + delta
        if not (0 <= idx < len(chs)) or not (0 <= j < len(chs)):
            return idx
        box = self._channels_el()
        box.remove(chs[idx].el)
        box.insert(j, chs[idx].el)
        self.dirty = True
        return j

    def sort_channels(self, by="freq"):
        """按频率重排（只动顺序，不动内容）"""
        box = self._channels_el()
        if box is None:
            return
        els = list(box.findall("CHANNEL"))
        key = (lambda e: _f(e.get(FREQ_ATTR[self.kind]))) if by == "freq" \
            else (lambda e: e.get("name", ""))
        for el in els:
            box.remove(el)
        for el in sorted(els, key=key):
            box.append(el)
        self.dirty = True

    # -- 保存
    def save(self, path=None, touch_date=True):
        """校验 + 原子写。返回实际写入路径。"""
        target = os.path.abspath(path or self.path or "")
        if not target:
            raise PlanError("没有指定保存路径")
        if touch_date:
            self.root.set("date", today_attr())
        # 表名与文件名保持一致（仪器用文件名列出表，不一致会对不上）
        base = os.path.splitext(os.path.basename(target))[0]
        if self.root.get("name") != base:
            self.root.set("name", base)
        self.root.set("band", KIND_BAND_ATTR[self.kind])

        d = os.path.dirname(target)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        data = serialize(self.tree)
        tmp = target + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, target)
        self.path = target
        self.dirty = False
        return target

    # -- 另存 / 改名 / 删除
    def save_as(self, name, source=None):
        """换个表名另存（同频段同目录）"""
        src_dir = os.path.dirname(self.path) if self.path else \
            os.path.join(source or "CH", KIND_DIRS[self.kind])
        newp = os.path.join(src_dir, _clean_name(name) + PLAN_EXT)
        if os.path.abspath(newp) == os.path.abspath(self.path or ""):
            raise PlanError("另存名与原文件相同：%s" % _clean_name(name))
        if os.path.exists(newp):
            raise PlanError("同名频道表已存在：%s" % os.path.basename(newp))
        return self.save(newp)

    def __repr__(self):
        return "<PlanFile %s %s %d ch>" % (self.kind, self.name, len(self))


def serialize(tree):
    """把 ElementTree 序列化成仪器同款格式

    - `<?xml version="1.0" encoding="utf-8"?>` + 换行（utf-8 **无 BOM**）
    - 每个元素一行、属性用双引号、自闭合标签写成 `<TAG/>`
    - 行尾 **LF**（仪器导出的文件是 LF；只有我们自己重排过的演示表是 LF）
    """
    root = tree.getroot()
    lines = ['<?xml version="1.0" encoding="utf-8"?>']
    _ser(root, lines, 0)
    return ("\n".join(lines) + "\n").encode("utf-8")


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _ser(el, out, depth):
    pad = "" if depth <= 1 else ""
    attrs = "".join(' %s="%s"' % (k, _esc(v)) for k, v in el.attrib.items())
    kids = list(el)
    text = (el.text or "").strip()
    if not kids and not text:
        out.append("%s<%s%s/>" % (pad, el.tag, attrs))
        return
    if not kids:
        out.append("%s<%s%s>%s</%s>" % (pad, el.tag, attrs, _esc(text), el.tag))
        return
    out.append("%s<%s%s>" % (pad, el.tag, attrs))
    for k in kids:
        _ser(k, out, depth + 1)
    out.append("%s</%s>" % (pad, el.tag))


# ---------------------------------------------------------------- 载入
def load_plan(path):
    """读一张频道表（保留整棵树，未知字段原样留着）"""
    if not os.path.isfile(path):
        raise PlanError("文件不存在：%s" % path)
    try:
        tree = ET.parse(path)
    except ET.ParseError as e:
        raise PlanError("XML 解析失败（%s）：%s" % (e, os.path.basename(path)))
    except OSError as e:
        raise PlanError("读取失败（%s）：%s" % (e, os.path.basename(path)))
    return PlanFile(path, tree, kind_of_path(path))


def new_plan(source, kind, name, lnb=None):
    """新建一张空频道表（内存里，save() 才落盘）

    lnb：卫星表的本振 MHz（C 波段默认 5150，Ku 默认 11300，按名字猜）
    """
    name = _clean_name(name)
    if not name:
        raise PlanError("表名不能为空")
    if len(name) > MAX_NAME_LEN:
        raise PlanError("表名太长（%d > %d）" % (len(name), MAX_NAME_LEN))
    if kind not in KINDS:
        raise PlanError("频道表类型只能是 TER 或 SAT：%r" % (kind,))
    target = os.path.join(source, KIND_DIRS[kind], name + PLAN_EXT)
    if os.path.exists(target):
        raise PlanError("同名频道表已存在：%s" % os.path.basename(target))

    root = ET.Element("CHANNEL-SET", {
        "band": KIND_BAND_ATTR[kind],
        "date": today_attr(),
        "equipment": "RANGERNeo 4",
        "name": name,
        "trademark": "PROMAX ELECTRONICA, S.L.",
        "version": "2.0",
    })
    header = ET.SubElement(root, "HEADER")
    if kind == KIND_TER:
        ET.SubElement(header, "TV", {"system": "PAL", "standard": "DK"})
        ET.SubElement(header, "LNB").text = "EXTERNAL"
    else:
        if lnb is None:
            lnb = 11300 if re.search(r"ku", name, re.I) else 5150
        osc = ET.SubElement(header, "LNB-LOC-OSC")
        tag = "BAND_KUKA" if float(lnb) >= 10000 else "BAND_C"
        if tag == "BAND_C":
            ET.SubElement(osc, tag, {"value": _fmt_sr(lnb), "units": "MHz"})
        else:
            ET.SubElement(osc, tag, {"high": _fmt_sr(lnb), "low": _fmt_sr(lnb),
                                     "units": "MHz"})
    ET.SubElement(root, "CHANNELS")
    tree = ET.ElementTree(root)
    return PlanFile(target, tree, kind)


# ---------------------------------------------------------------- 列表 / 维护
def list_plans(source):
    """列出 source 下 TER/、SAT/ 里所有频道表

    返回 [{kind, path, name, count, mtime, error}]（按名字排序）
    """
    out = []
    for kind in KINDS:
        d = os.path.join(source, KIND_DIRS[kind])
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.upper().endswith(PLAN_EXT):
                continue
            p = os.path.join(d, fn)
            item = {"kind": kind, "path": p,
                    "name": os.path.splitext(fn)[0],
                    "count": 0,
                    "mtime": os.path.getmtime(p),
                    "error": ""}
            try:
                item["count"] = len(load_plan(p))
            except PlanError as e:
                item["error"] = str(e)
            out.append(item)
    return out


def delete_plan(path):
    """删除频道表文件（调用方负责先备份）"""
    if not os.path.isfile(path):
        raise PlanError("文件不存在：%s" % path)
    os.remove(path)


def rename_plan(path, new_name):
    """改名：文件名 + 文件内 name 属性一起改（否则仪器列出来的名字对不上）"""
    new_name = _clean_name(new_name)
    if not new_name:
        raise PlanError("新表名不能为空")
    d = os.path.dirname(path)
    newp = os.path.join(d, new_name + PLAN_EXT)
    if os.path.abspath(newp) == os.path.abspath(path):
        return path
    if os.path.exists(newp):
        raise PlanError("同名频道表已存在：%s" % os.path.basename(newp))
    p = load_plan(path)
    p.name = new_name
    p.save(newp)
    os.remove(path)
    return newp


def copy_plan(path, new_name):
    """复制一张表（同名冲突时的"另存新名"用它）"""
    d = os.path.dirname(path)
    newp = os.path.join(d, _clean_name(new_name) + PLAN_EXT)
    if os.path.exists(newp):
        raise PlanError("同名频道表已存在：%s" % os.path.basename(newp))
    p = load_plan(path)
    p.name = _clean_name(new_name)
    return p.save(newp)


def suggest_name(base, exists):
    """在同名冲突时给个可用名字：base_2 / base_3 …"""
    base = _clean_name(base) or "plan"
    if base not in exists:
        return base
    n = 2
    while ("%s_%d" % (base, n)) in exists:
        n += 1
    return "%s_%d" % (base, n)
