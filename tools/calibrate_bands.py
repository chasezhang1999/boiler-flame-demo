"""
按台账里的真实分布重新标定参考区间。

区间是「相对本机位是否异常」的判据，强依赖摄像头位置、视野和曝光——
换个机位就得重标，所以做成工具而不是手改常量。

    python tools/calibrate_bands.py                # 看当前分布和建议
    python tools/calibrate_bands.py --width 90     # 放宽到 P5~P95
    python tools/calibrate_bands.py --width 90 --apply

--width 是保留在区间内的百分比：80 → P10~P90，90 → P5~P95，越大越松。

有两项**不按分布标**，因为「常见」不等于「正常」：

  贴壁亮斑面积 / 边缘带高温像素 —— 这是结焦最直接的信号。素材里贴壁亮斑
  很常见，按 P95 放宽会把最该报警的东西压成「正常」，等于把报警器关了。
  这两项只跟着 --width 轻微放松，且有下限保护。
"""

import argparse
import json
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from service import ledger, report   # noqa: E402

# 这两项不按分布走，见模块开头说明
PROTECTED = {
    "wall_hotspot_area_pct": (0.3, 1.5),      # (最严, 最松) 随 width 在区间内插值
    "hot_pixels_near_edge_pct": (10.0, 30.0),
    # 偏斜同理：这批素材最大才 67%，按 P95 放到 60 等于这项永远不报警了，
    # 而它是「火焰偏烧、贴壁」的主要指标，判据表里权重很高。
    "centroid_offset_total_pct": (30.0, 45.0),
}


def pct(vals, p):
    v = sorted(vals)
    if not v:
        return None
    k = (len(v) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def nice(x):
    """把区间端点圆到好读的数，别出现 0.7834 这种。"""
    if x is None:
        return None
    a = abs(x)
    if a >= 10:
        return round(x)
    if a >= 1:
        return round(x, 1)
    return round(x, 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--width", type=float, default=90,
                    help="区间内保留的百分比，越大越松（默认 90 = P5~P95）")
    ap.add_argument("--apply", action="store_true", help="写回 service/report.py")
    a = ap.parse_args()

    lo_p = (100 - a.width) / 2.0
    hi_p = 100 - lo_p

    c = sqlite3.connect(ledger.DB_PATH)
    rows = [json.loads(r[0]) for r in c.execute("SELECT metrics_json FROM runs")]
    if not rows:
        raise SystemExit("台账是空的，没法标定")

    print("样本 %d 条，区间取 P%g~P%g\n" % (len(rows), lo_p, hi_p))
    print("%-24s %14s   %14s" % ("指标", "现区间", "建议区间"))
    print("-" * 60)

    new_bands = {}
    for key, lab, unit, span, band, low_w, high_w, signed in report.METRICS:
        if band is None:
            continue
        vals = [abs(r[key]) for r in rows if r.get(key) is not None]
        if not vals:
            continue

        if key in PROTECTED:
            tight, loose = PROTECTED[key]
            # width 80→最严，100→最松，中间线性插值
            f = max(0.0, min(1.0, (a.width - 80) / 20.0))
            nb = (0, nice(tight + (loose - tight) * f))
        elif span is not None and band[1] >= span:
            # 上限顶到量程 = 这一项「越大越好」，上限不是阈值而是天花板
            # （圆度就是，1 表示正圆）。只放松下限，别把完美的读数判成异常。
            nb = (nice(pct(vals, lo_p)), band[1])
        elif signed or band[0] == 0:
            # 单边指标：只有上限，下限恒为 0
            nb = (0, nice(pct(vals, hi_p)))
        else:
            # 双边：放松只能变宽，不能反而收窄
            lo = min(nice(pct(vals, lo_p)), band[0])
            hi = max(nice(pct(vals, hi_p)), band[1])
            nb = (lo, hi)

        new_bands[key] = nb
        mark = "  ←保护项" if key in PROTECTED else ""
        print("%-24s %14s → %14s%s" % (lab[:12], str(band), str(nb), mark))

    # 用新区间回算超限项分布
    print()
    orig = {k: v["band"] for k, v in report.METRIC_BY_KEY.items()}
    for name, bands in (("现区间", orig), ("建议区间", None)):
        if bands is None:
            for k, nb in new_bands.items():
                report.METRIC_BY_KEY[k]["band"] = nb
            report.METRICS[:] = [
                (k, l, u, s, new_bands.get(k, b), lw, hw, sg)
                for k, l, u, s, b, lw, hw, sg in report.METRICS
            ]
        cnt = {}
        for m in rows:
            n = report.build_context('{"risk_level":"中"}', m, "", "", "")["abnormal_count"]
            cnt[n] = cnt.get(n, 0) + 1
        avg = sum(k * v for k, v in cnt.items()) / len(rows)
        zero = cnt.get(0, 0)
        print("%s：平均 %.1f 项超限，%d/%d 条零超限  %s"
              % (name, avg, zero, len(rows),
                 " ".join("%d项:%d" % (k, cnt[k]) for k in sorted(cnt))))

    if not a.apply:
        print("\n加 --apply 写回 service/report.py")
        return 0

    p = os.path.join(ROOT, "service", "report.py")
    src = open(p, encoding="utf-8").read()
    for key, nb in new_bands.items():
        # 只替换该行里的区间元组，别动标签和说法
        pat = re.compile(r'(\("%s",\s+"[^"]*",\s+"[^"]*",\s+[\w.]+,\s+)\([^)]*\)' % key)
        src, n = pat.subn(lambda m: m.group(1) + str(nb), src, count=1)
        if not n:
            print("  !! 没匹配上 %s，需要手改" % key)
    open(p, "w", encoding="utf-8", newline="\n").write(src)
    print("\n已写回 service/report.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
