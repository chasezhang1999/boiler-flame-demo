"""
机组 / 位置清单 —— 全系统唯一的事实来源。

台账按 id 存，前端下拉、历史筛选、趋势图都从这里取。Dify 工作流里那份
下拉选项读不到接口，只能写死在 DSL 中，用 tools/sync_sites.py 从这里生成，
别手改，否则两边会漂移。

demo 用的模拟数据。换成真实机组时改这里，然后：
  1. python tools/sync_sites.py        同步 DSL 里的选项
  2. 台账里的历史 site 字段要一并迁移，否则旧记录查不出来
"""

# (id, 机组, 位置, 说明)
_RAW = [
    ("u1", "1号机组", ["A层燃烧器", "B层燃烧器", "C层燃烧器", "观火平台"]),
    ("u2", "2号机组", ["A层燃烧器", "B层燃烧器", "C层燃烧器", "观火平台"]),
    ("u3", "3号机组", ["A层燃烧器", "B层燃烧器", "C层燃烧器", "观火平台"]),
    ("u4", "4号机组", ["A层燃烧器", "B层燃烧器", "C层燃烧器", "观火平台"]),
]

_SPOT_ID = {
    "A层燃烧器": "a", "B层燃烧器": "b", "C层燃烧器": "c", "观火平台": "out",
}

SITES = [
    {
        "id": "%s-%s" % (uid, _SPOT_ID[spot]),
        "unit": unit,
        "spot": spot,
        "label": "%s · %s" % (unit, spot),
    }
    for uid, unit, spots in _RAW
    for spot in spots
]

BY_ID = {s["id"]: s for s in SITES}
LABELS = [s["label"] for s in SITES]
BY_LABEL = {s["label"]: s for s in SITES}


def _aliases(s):
    """一个机位的几种叫法。用户很少写全「B层燃烧器」，多半只说「B层」。"""
    u = s["unit"].replace(" ", "")
    sp = s["spot"].replace(" ", "")
    out = {u + sp}
    if sp.endswith("燃烧器"):
        out.add(u + sp[:-3])          # 3号机组B层
    return out


def resolve(value: str):
    """
    把各种写法归一到一条 site 记录：id、完整 label、片段（「3号机组B层」），
    以及**整句话里点名了某个机位**的情况（「3号机组B层最近怎么样？」）。

    最后这种以前是认不出来的：原来只判「查询是不是机位名的一部分」，
    方向反了 —— 句子总比机位名长，于是永远匹配不上，问答就悄悄退回全量台账。

    匹配不上返回 None，调用方自己决定怎么办 —— 不要瞎猜，猜错了台账就串号了。
    """
    if not value:
        return None
    v = str(value).strip()
    if v in BY_ID:
        return BY_ID[v]
    if v in BY_LABEL:
        return BY_LABEL[v]

    flat = v.replace(" ", "").replace("·", "").replace("-", "").upper()
    if not flat:
        return None

    # 整句里点名机位：取最长命中，避免「3号机组B层」被「3号机组」抢先
    best = None
    for s in SITES:
        for a in _aliases(s):
            key = a.upper()
            if key in flat and (best is None or len(key) > len(best[0])):
                best = (key, s)
    if best:
        return best[1]

    # 用户只输了片段（「3号机组B」），反过来按前缀找，必须唯一才算数
    hits = [s for s in SITES
            if any(a.upper().startswith(flat) for a in _aliases(s))]
    return hits[0] if len(hits) == 1 else None


# 机组级别（一台机组下辖多个机位）。「3号机组最近怎么样」问的是整台机组，
# 这是最自然的问法，只能定位到单个机位的话会退回全量台账，答非所问。
UNITS = [{"id": uid, "label": unit} for uid, unit, _ in _RAW]
BY_UNIT = {u["id"]: u for u in UNITS}


def resolve_scope(value: str) -> dict:
    """
    把一段文字解析成查询范围。三种结果：

      {"kind":"site", "id":"u3-b", "label":"3号机组 · B层燃烧器"}   具体机位
      {"kind":"unit", "id":"u3",   "label":"3号机组（全部机位）"}    整台机组
      {"kind":"all",  "id":"",     "label":"全部机组"}             没点名

    先找机位再找机组：说了「3号机组B层」就该定位到那个机位，
    只说「3号机组」才按整台算。
    """
    s = resolve(value)
    if s:
        return {"kind": "site", "id": s["id"], "label": s["label"]}

    v = str(value or "").strip()
    if v in BY_UNIT:
        u = BY_UNIT[v]
        return {"kind": "unit", "id": u["id"], "label": u["label"] + "（全部机位）"}

    flat = v.replace(" ", "").replace("·", "").replace("-", "")
    hits = [u for u in UNITS if u["label"] in flat]
    if len(hits) == 1:
        return {"kind": "unit", "id": hits[0]["id"],
                "label": hits[0]["label"] + "（全部机位）"}
    return {"kind": "all", "id": "", "label": "全部机组"}


def label_of(site_id: str) -> str:
    """机位 id、机组 id 都认，方便台账里的记录和筛选条件共用一个显示函数。"""
    s = BY_ID.get(site_id)
    if s:
        return s["label"]
    u = BY_UNIT.get(site_id)
    if u:
        return u["label"] + "（全部机位）"
    return site_id or "未指定"
