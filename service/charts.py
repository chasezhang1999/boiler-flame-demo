"""
趋势图：手排 SVG，不引 matplotlib。

不用 matplotlib 的两个原因：一是为一张折线图拖进来一个几十兆的依赖不划算；
二是容器里没有中文字体，matplotlib 画中文会出一堆方框，还得再装字体。
SVG 是文本，字体交给浏览器，报告页已经自托管了 Barlow，正好复用。

配色沿用报告页那套：判级色 + 越界色，别再引入第三套。
"""

import html

W, H = 720, 260                     # 画布
PAD_L, PAD_R, PAD_T, PAD_B = 52, 16, 22, 34

INK = "#1d1f20"
MUTED = "#5d5d60"
FAINT = "#98989b"
LINE = "#e3e3e6"
ACCENT = "#416180"
LEVEL_COLOR = {"高": "#e0483a", "中": "#fbaf17", "低": "#22a35c", "未知": "#7a7a7d"}

FONT = ('font-family="Barlow,-apple-system,Segoe UI,Microsoft YaHei,sans-serif"')


def _esc(s):
    return html.escape(str(s), quote=True)


def _shell(body, w=W, h=H, title=""):
    t = ('<title>%s</title>' % _esc(title)) if title else ""
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d" %s>%s'
        '<rect width="%d" height="%d" fill="#fff"/>%s</svg>'
        % (w, h, w, h, FONT, t, w, h, body)
    )


def _nice_bounds(vals):
    """给一组数配一个好看的上下界，避免折线贴边或全挤在中间。"""
    if not vals:
        return 0.0, 1.0
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        pad = abs(hi) * 0.15 or 0.5
        return lo - pad, hi + pad
    pad = (hi - lo) * 0.15
    return lo - pad, hi + pad


def _fmt(v):
    if v is None:
        return "-"
    return ("%g" % round(v, 3)) if abs(v) < 10 else ("%.1f" % v)


def line_chart(points, label="", unit="", band=None, title=""):
    """
    折线图。points = [{"ts": "...", "v": 数值}, ...]，已按时间升序。
    band = (下限, 上限) 时在背景画一条参考带。
    """
    if not points:
        return _shell(
            '<text x="%d" y="%d" text-anchor="middle" font-size="13" fill="%s">'
            '台账里还没有该条件下的记录</text>' % (W // 2, H // 2, MUTED),
            title=title)

    vals = [p["v"] for p in points]
    lo, hi = _nice_bounds(vals + ([band[0], band[1]] if band else []))
    iw, ih = W - PAD_L - PAD_R, H - PAD_T - PAD_B

    def X(i):
        return PAD_L + (iw * i / max(len(points) - 1, 1))

    def Y(v):
        return PAD_T + ih - (v - lo) / (hi - lo) * ih

    out = []

    # 参考带
    if band:
        y1, y2 = Y(band[1]), Y(band[0])
        out.append('<rect x="%.1f" y="%.1f" width="%d" height="%.1f" fill="#eef3f7"/>'
                   % (PAD_L, y1, iw, max(y2 - y1, 1)))

    # 横向网格 + 纵轴刻度
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        y = Y(v)
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s"/>'
                   % (PAD_L, y, W - PAD_R, y, LINE))
        out.append('<text x="%d" y="%.1f" text-anchor="end" font-size="10.5" '
                   'fill="%s">%s</text>' % (PAD_L - 7, y + 3.5, FAINT, _fmt(v)))

    # 折线
    d = " ".join("%s%.1f %.1f" % ("M" if i == 0 else "L", X(i), Y(p["v"]))
                 for i, p in enumerate(points))
    out.append('<path d="%s" fill="none" stroke="%s" stroke-width="2" '
               'stroke-linejoin="round"/>' % (d, ACCENT))

    # 数据点；越出参考带的标红
    for i, p in enumerate(points):
        bad = band and not (band[0] <= p["v"] <= band[1])
        out.append('<circle cx="%.1f" cy="%.1f" r="%s" fill="%s"/>'
                   % (X(i), Y(p["v"]), "3.6" if bad else "2.6",
                      LEVEL_COLOR["高"] if bad else ACCENT))

    # 横轴：只标首尾和中间，标多了挤成一团
    marks = {0, len(points) - 1} | ({len(points) // 2} if len(points) > 2 else set())
    for i in sorted(marks):
        out.append('<text x="%.1f" y="%d" text-anchor="middle" font-size="10.5" '
                   'fill="%s">%s</text>'
                   % (X(i), H - 12, FAINT, _esc(points[i]["ts"][5:16])))

    head = "%s%s · 共 %d 次" % (label, ("（%s）" % unit if unit else ""), len(points))
    out.append('<text x="%d" y="14" font-size="12" fill="%s">%s</text>'
               % (PAD_L, MUTED, _esc(head)))
    return _shell("".join(out), title=title or label)


def level_bars(rows, title=""):
    """
    风险等级分布堆叠条。rows = [{"label":.., "高":n, "中":n, "低":n}, ...]
    """
    if not rows:
        return _shell('<text x="%d" y="%d" text-anchor="middle" font-size="13" '
                      'fill="%s">台账里还没有记录</text>' % (W // 2, H // 2, MUTED),
                      title=title)

    bar_h, gap = 22, 12
    h = PAD_T + len(rows) * (bar_h + gap) + 26
    iw = W - PAD_L - PAD_R - 60
    out = []
    for i, r in enumerate(rows):
        y = PAD_T + i * (bar_h + gap)
        total = sum(r.get(k, 0) for k in ("高", "中", "低")) or 1
        x = PAD_L
        for k in ("高", "中", "低"):
            n = r.get(k, 0)
            if not n:
                continue
            w = iw * n / total
            out.append('<rect x="%.1f" y="%d" width="%.1f" height="%d" fill="%s"/>'
                       % (x, y, w, bar_h, LEVEL_COLOR[k]))
            if w > 22:
                out.append('<text x="%.1f" y="%d" text-anchor="middle" font-size="11" '
                           'fill="#fff">%d</text>' % (x + w / 2, y + 15, n))
            x += w
        out.append('<text x="%d" y="%d" text-anchor="end" font-size="11.5" fill="%s">'
                   '%s</text>' % (PAD_L - 8, y + 15, INK, _esc(r["label"])))
        out.append('<text x="%d" y="%d" font-size="11" fill="%s">%d 次</text>'
                   % (PAD_L + iw + 8, y + 15, MUTED, total))

    lx = PAD_L
    for k in ("高", "中", "低"):
        out.append('<rect x="%d" y="%d" width="10" height="10" fill="%s"/>'
                   % (lx, h - 18, LEVEL_COLOR[k]))
        out.append('<text x="%d" y="%d" font-size="11" fill="%s">%s风险</text>'
                   % (lx + 14, h - 9, MUTED, k))
        lx += 74
    return _shell("".join(out), h=h, title=title or "风险等级分布")
