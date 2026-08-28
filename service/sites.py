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
    ("u1", "1号机组", ["A层燃烧器", "B层燃烧器", "C层燃烧器", "炉膛出口"]),
    ("u2", "2号机组", ["A层燃烧器", "B层燃烧器", "C层燃烧器", "炉膛出口"]),
    ("u3", "3号机组", ["A层燃烧器", "B层燃烧器", "C层燃烧器", "炉膛出口"]),
    ("u4", "4号机组", ["A层燃烧器", "B层燃烧器", "C层燃烧器", "炉膛出口"]),
]

_SPOT_ID = {
    "A层燃烧器": "a", "B层燃烧器": "b", "C层燃烧器": "c", "炉膛出口": "out",
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


def resolve(value: str):
    """
    把各种写法归一到一条 site 记录：id、完整 label、或者用户随口说的
    「3号机组B层」这类片段。匹配不上返回 None，调用方自己决定怎么办 ——
    不要瞎猜，猜错了台账就串号了。
    """
    if not value:
        return None
    v = str(value).strip()
    if v in BY_ID:
        return BY_ID[v]
    if v in BY_LABEL:
        return BY_LABEL[v]

    # 去掉分隔符和空格后做包含匹配，"3号机组B层" 能命中 "3号机组 · B层燃烧器"
    flat = v.replace(" ", "").replace("·", "").replace("-", "").upper()
    hits = []
    for s in SITES:
        key = (s["unit"] + s["spot"]).replace(" ", "").upper()
        if flat and (flat in key or key.startswith(flat)):
            hits.append(s)
    return hits[0] if len(hits) == 1 else None


def label_of(site_id: str) -> str:
    s = BY_ID.get(site_id)
    return s["label"] if s else (site_id or "未指定")
