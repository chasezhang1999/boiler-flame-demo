"""
报告渲染：把 CV 指标和模型判级整理成上下文，交给 templates/ 下的模板出稿。

这里只管数据准备，版式全在模板里：
  templates/report.html.j2   独立的 HTML 报告页
  templates/report.md.j2     工作流回给用户的 markdown 正文

改版式改模板就行，不用动这个文件；容器重启即生效。

报告单独成页而不是塞回 Dify 的输出框，是因为 Dify 的 markdown 渲染器对原始 HTML
是否放行不好保证，而且报告要能直接转发、打印、投屏。

"""

import json
import os
import re

from jinja2 import Environment, FileSystemLoader, StrictUndefined

TPL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

_env = Environment(
    loader=FileSystemLoader(TPL_DIR),
    autoescape=True,          # HTML 模板要转义，模型输出里可能带尖括号
    undefined=StrictUndefined,  # 变量写错名就报错，别悄悄渲染成空白
    trim_blocks=False,
    lstrip_blocks=False,
)

# markdown 不能自动转义，否则中文引号、&amp; 之类会被转成实体
_env_md = Environment(
    loader=FileSystemLoader(TPL_DIR),
    autoescape=False,
    undefined=StrictUndefined,
)

# 判级配色。中风险用偏亮的琥珀色，浅底上比 #d68910 跳得出来。
# ink = 主色（色块底、左边条），tint = 结论条底色，line = 结论条描边
LEVELS = {
    "高": {"ink": "#e0483a", "tint": "#fdedec", "line": "#f3c4bf",
           "label": "高风险", "badge": "🔴 高风险"},
    "中": {"ink": "#fbaf17", "tint": "#fff8ec", "line": "#f5dcaa",
           "label": "中风险", "badge": "🟠 中风险"},
    "低": {"ink": "#22a35c", "tint": "#eefaf3", "line": "#bfe6d1",
           "label": "低风险", "badge": "🟢 低风险"},
}
UNKNOWN = {"ink": "#7a7a7d", "tint": "#f5f5f8", "line": "#d4d4d7",
           "label": "判级未知", "badge": "⚪ 判级未知"}

# 越界配色：低于参考区间用蓝、高于用橙，两个方向必须一眼分得开
COLOR_LOW = "#1f6d9c"
COLOR_HIGH = "#c2410c"
COLOR_OK = "#5d5d60"
COLOR_INK = "#1d1f20"

# 有符号偏移量的显示满程（%）。用 ±100 的话 ±10 的参考带细到看不见。
DISPLAY_SPAN = 50

# key, 中文名, 单位, 量程（None = 不画条）, 参考区间（None = 不判越界）,
# 低于区间时的说法, 高于区间时的说法, 是否有符号（正负号表示方向）
#
# 量程只用来算条形图宽度；参考区间才是判「正常/偏高/偏低」的依据。
#
# 区间分两类，不能一把尺子量到底：
#
#   取景相关的（充满度、亮温、圆度、高温区占比）—— 强烈依赖摄像头位置、
#   视野和曝光。用真实台账数据按分位数标定，含义是「相对本机位是否异常」，
#   换个摄像头要重标：python tools/calibrate_bands.py --width 90 --apply
#
#   物理意义明确的（贴壁亮斑、边缘带高温、火焰中心偏斜）—— 不按分布标。
#   这批素材里贴壁亮斑很常见、偏斜也普遍偏大，但「常见」不等于「正常」：
#   按 P95 放宽会把最该报警的信号压成正常，等于把报警器关掉。这三项只随
#   width 轻微放松，且有上限保护，见 tools/calibrate_bands.py 的 PROTECTED。
METRICS = [
    ("flame_fill_ratio_pct",      "火焰充满度",         "%", 100, (9.3, 34),    "偏低", "偏高", False),
    ("centroid_offset_total_pct", "火焰中心偏斜",       "%", 100, (0, 38),     "",     "偏大", False),
    ("centroid_offset_x_pct",     "水平偏移（负=偏左）", "%", 100, (0, 55),     "偏左", "偏右", True),
    ("centroid_offset_y_pct",     "垂直偏移（负=偏上）", "%", 100, (0, 35),     "偏上", "偏下", True),
    ("temp_index_mean",           "相对亮温均值",       "",  1,   (0.7, 0.8), "偏低", "偏高", False),
    ("temp_index_p95",            "相对亮温 P95",       "",  1,   (0.78, 0.88), "偏低", "偏高", False),
    ("high_temp_area_pct",        "高温区占比",         "%", 100, (16, 84),    "偏小", "偏大", False),
    ("contour_circularity",       "轮廓圆度（1=正圆）",  "",  1,   (0.18, 1),   "破碎", "",     False),
    ("hot_pixels_near_edge_pct",  "边缘带高温像素",     "%", 100, (0, 20),     "",     "偏高", False),
    ("wall_hotspot_count",        "贴壁亮斑数",         "处", None, None,       "",     "检出", False),
    ("wall_hotspot_area_pct",     "贴壁亮斑面积",       "%", 100, (0, 0.9),    "",     "偏高", False),
]

# 按 key 查一项指标的元信息，趋势图要用（标签、单位、参考区间）
METRIC_BY_KEY = {
    key: {"label": lab, "unit": unit, "span": span, "band": band,
          "low_word": low, "high_word": high, "signed": signed}
    for key, lab, unit, span, band, low, high, signed in METRICS
}

# markdown 里只列关键几项，全部指标留给 HTML 报告页
MD_KEYS = [
    "flame_fill_ratio_pct", "centroid_offset_total_pct", "temp_index_p95",
    "high_temp_area_pct", "contour_circularity", "wall_hotspot_count",
]

SPARK_W = 10          # markdown 里字符条的格数


def strip_think(text: str) -> str:
    """
    剥掉模型的 <think> 思考块。

    对判级来说这一步不能省：定位 JSON 靠的是第一个 '{'，而模型在思考里完全
    可能写出花括号（它正在琢磨要输出什么 JSON），那样就会从错的位置开始截。
    对话问答里同样要剥，否则思考过程会直接显示给用户。
    """
    raw = (text or "").strip()
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S).strip()
    return re.sub(r"^<think>.*", "", raw, flags=re.S).strip()   # 思考没闭合的情况


def parse_assessment(text: str) -> dict:
    """模型偶尔会用 ```json 裹输出，或在 JSON 前后加话，都剥掉。"""
    raw = strip_think(text)

    m = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
    if m:
        raw = m.group(1).strip()
    else:
        i, j = raw.find("{"), raw.rfind("}")
        if i != -1 and j > i:
            raw = raw[i:j + 1]
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _clean(items):
    # 模型偶尔把该给数组的字段写成一个字符串。不拦的话下面的遍历会把它
    # 拆成一个字一条，列表直接炸成「火」「焰」「偏」「斜」。
    if isinstance(items, str):
        items = [items]
    return [str(x).strip() for x in (items or []) if str(x).strip()]


def _first_text(value):
    """headline 这类「一句话」字段：模型可能给字符串，也可能给单元素数组。"""
    if isinstance(value, str):
        return value.strip()
    items = _clean(value)
    return items[0] if items else ""


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct(value, span):
    """把数值折算成条形图宽度（0~100）。取不到数就返回 None，模板据此不画条。"""
    if span is None:
        return None
    v = _num(value)
    if v is None:
        return None
    return round(max(0.0, min(100.0, abs(v) / span * 100)), 1)


def _signed_pos(value, unit):
    """双向轴上的位置（0~100，50 = 零点）。"""
    v = _num(value)
    if v is None:
        return None
    span = DISPLAY_SPAN if unit == "%" else 1.0
    return round(50 + max(-50.0, min(50.0, v / span * 50)), 1)


def _judge(value, band, signed):
    """-1 低于参考区间 / 0 区间内 / 1 高于。band 为 None 时按「有就是关注」处理。"""
    v = _num(value)
    if v is None:
        return 0
    if band is None:
        return 1 if v > 0 else 0
    if signed:                      # 条按绝对值画，方向由正负号给
        return 0 if abs(v) <= band[1] else (-1 if v < 0 else 1)
    if v < band[0]:
        return -1
    if v > band[1]:
        return 1
    return 0


def _spark(pct, band_pct=None, mark_pct=None):
    """markdown 用的字符条：▬ 是参考区间，▮ 是本次读数，· 是量程其余部分。"""
    if pct is None and mark_pct is None:
        return ""
    cells = ["·"] * SPARK_W
    if band_pct:
        lo = int(band_pct[0] / 100 * SPARK_W)
        hi = max(lo + 1, int(round(band_pct[1] / 100 * SPARK_W)))
        for i in range(max(0, lo), min(SPARK_W, hi)):
            cells[i] = "▬"
    pos = mark_pct if mark_pct is not None else pct
    idx = min(SPARK_W - 1, max(0, int(round(pos / 100 * (SPARK_W - 1)))))
    cells[idx] = "▮"
    return "".join(cells)


def _range_text(band, unit, signed):
    if band is None:
        return "无量程"
    if signed:
        return "±%g%s" % (band[1], unit)
    return "%g–%g%s" % (band[0], band[1], unit)


def build_context(assessment_text: str, metrics: dict, contour_url: str,
                  heatmap_url: str, report_url: str = "",
                  shot_at: str = "", site: str = "") -> dict:
    """两套模板共用的上下文。"""
    a = parse_assessment(assessment_text)
    level = str(a.get("risk_level", "")).strip()
    lv = LEVELS.get(level, UNKNOWN)

    try:
        score = max(0, min(100, int(a.get("score"))))
    except (TypeError, ValueError):
        score = None

    # 图像观察和指标推断分开呈现：模型拿到的是「照片 + 指标」，
    # 混在一起写就分不清哪句是真看到的、哪句是照着判据表套出来的。
    visual = _clean(a.get("visual_findings"))
    metric = _clean(a.get("metric_findings"))
    if not visual and not metric:          # 兼容旧版只有 findings 的输出
        metric = _clean(a.get("findings"))

    rows = []
    for key, lab, unit, span, band, low_word, high_word, signed in METRICS:
        if key not in metrics:
            continue
        value = metrics[key]
        out = _judge(value, band, signed)
        pct = _pct(value, span)

        if signed and band is not None:
            band_lo = _signed_pos(-band[1], unit)
            band_hi = _signed_pos(band[1], unit)
            mark = _signed_pos(value, unit)
        elif band is not None and span is not None:
            band_lo = round(band[0] / span * 100, 1)
            band_hi = round(band[1] / span * 100, 1)
            mark = pct
        else:
            band_lo = band_hi = mark = None

        state = low_word or "偏低" if out == -1 else (
            high_word or "偏高" if out == 1 else "区间内")
        color = COLOR_LOW if out == -1 else COLOR_HIGH if out == 1 else COLOR_OK

        rows.append({
            "key": key, "label": lab, "unit": unit, "value": value,
            "pct": pct, "has_bar": mark is not None,
            "band": band, "signed": signed, "zero_tick": bool(signed),
            "band_left": band_lo, "band_width": None if band_lo is None else round(band_hi - band_lo, 1),
            "mark_left": mark, "out": out, "abnormal": out != 0,
            "state": state, "color": color,
            "value_color": color if out != 0 else COLOR_INK,
            "mark_color": color if out != 0 else COLOR_INK,
            "mark_width": 4 if out != 0 else 2,
            "range_text": _range_text(band, unit, signed),
            "arrow": "↓" if out == -1 else "↑" if out == 1 else "",
        })

    md_rows = [
        dict(r, spark=_spark(r["pct"],
                             None if r["band_left"] is None else (r["band_left"], r["band_left"] + r["band_width"]),
                             r["mark_left"]))
        for r in rows if r["key"] in MD_KEYS
    ]

    hotspots = [
        {"pos": s.get("位置", "-"), "area": s.get("面积占比%", "-"),
         "temp": s.get("相对亮温", "-")}
        for s in (metrics.get("wall_hotspots") or [])
    ]

    abnormal = [r for r in rows if r["abnormal"]]

    return {
        "level": level or "未知", "level_label": lv["label"], "badge": lv["badge"],
        "color": lv["ink"], "bg": lv["tint"], "line": lv["line"],
        "levels": [
            {"key": k, "label": k, "ink": LEVELS[k]["ink"],
             "line": LEVELS[k]["line"], "active": k == level}
            for k in ("低", "中", "高")
        ],
        "color_low": COLOR_LOW, "color_high": COLOR_HIGH,
        "color_ok": COLOR_OK, "color_ink": COLOR_INK,
        "score": score,
        "meta": " · ".join([x for x in (site, shot_at) if x]),
        "site": site, "shot_at": shot_at,
        "parsed": bool(a), "raw_text": assessment_text or "（空）",
        "visual": visual, "metric": metric,
        "reasons": _clean(a.get("reasons")),
        "suggestions": _clean(a.get("suggestions")),
        "metrics": rows, "md_metrics": md_rows, "hotspots": hotspots,
        "abnormal": abnormal, "abnormal_count": len(abnormal),
        "headline": _first_text(a.get("headline")),
        "metrics_json": json.dumps(metrics, ensure_ascii=False, indent=2),
        "contour_url": contour_url, "heatmap_url": heatmap_url,
        "report_url": report_url,
    }


def render_html(ctx: dict) -> str:
    return _env.get_template("report.html.j2").render(**ctx)


def render_markdown_from(ctx: dict) -> str:
    md = _env_md.get_template("report.md.j2").render(**ctx)
    # 模板里的空白控制符逐处调准太琐碎，渲染完统一压掉连续空行
    return re.sub(r"\n{3,}", "\n\n", md).strip() + "\n"


# ---------------------------------------------------------------- 兼容旧调用

def render(assessment_text: str, metrics: dict, contour_url: str,
           heatmap_url: str, shot_at: str = "", site: str = "") -> tuple:
    ctx = build_context(assessment_text, metrics, contour_url, heatmap_url,
                        "", shot_at, site)
    return render_html(ctx), ctx["level"]


def render_markdown(assessment_text: str, metrics: dict, contour_url: str,
                    heatmap_url: str, report_url: str,
                    shot_at: str = "", site: str = "") -> str:
    ctx = build_context(assessment_text, metrics, contour_url, heatmap_url,
                        report_url, shot_at, site)
    return render_markdown_from(ctx)
