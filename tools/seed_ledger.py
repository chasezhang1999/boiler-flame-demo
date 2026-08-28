"""
往台账里灌模拟历史，让趋势图和问答有东西可查。

不这么做的话，历史页和问答页打开都是空的 —— 演示当天没人有耐心先拍二十张照片。

这些是**编出来的数**，不是真实巡检记录。每台机组给一个自己的「性格」
（基线 + 漂移方向），再叠随机噪声，这样趋势图看起来像回事，
横向对比也能分出好坏，不至于每台都长一个样。

    python tools/seed_ledger.py --days 45 --per-day 2
    python tools/seed_ledger.py --wipe        # 清空重来
"""

import argparse
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service import ledger, sites   # noqa: E402

# 每台机组的基线：(充满度, 偏斜, 亮温P95, 高温区占比, 圆度, 贴壁面积)
# 以及这台机组在观察期内的漂移方向 —— 有的越来越差，有的稳定。
PROFILES = {
    "u1": dict(base=(38, 6, 0.62, 12, 0.78, 0.05), drift=+0.15, name="平稳偏好"),
    "u2": dict(base=(30, 11, 0.71, 18, 0.66, 0.35), drift=+0.55, name="持续恶化"),
    "u3": dict(base=(44, 4, 0.58, 9, 0.83, 0.02), drift=-0.10, name="良好"),
    "u4": dict(base=(26, 14, 0.76, 24, 0.55, 0.90), drift=+0.25, name="长期偏差"),
}

HEADLINES = {
    "高": ["火焰明显偏斜并检出贴壁亮斑，建议尽快现场核查",
           "焰心温度偏高且高温区集中，结焦风险上升",
           "火焰形态破碎，燃烧不稳，壁面已有发亮迹象"],
    "中": ["火焰中心存在偏移，建议复核配风",
           "局部高温区占比偏大，需持续观察",
           "轮廓略破碎，整体尚在可接受范围"],
    "低": ["火焰形态完整，各项指标在参考区间内",
           "燃烧稳定，未检出贴壁亮斑"],
}


def _jitter(v, pct=0.18):
    return v * (1 + random.uniform(-pct, pct))


def _grade(m):
    """按指标偏离程度定级。跟报告里的参考区间口径保持一致。"""
    bad = 0
    if m["flame_fill_ratio_pct"] < 25:            bad += 1
    if m["centroid_offset_total_pct"] > 15:       bad += 1
    if m["temp_index_p95"] > 0.85:                bad += 1
    if m["high_temp_area_pct"] > 25:              bad += 1
    if m["contour_circularity"] < 0.6:            bad += 1
    if m["wall_hotspot_area_pct"] > 0.3:          bad += 2      # 贴壁权重最高
    if bad >= 4:
        return "高", random.randint(72, 92), bad
    if bad >= 2:
        return "中", random.randint(45, 68), bad
    return "低", random.randint(12, 38), bad


def make_run(site_id, day_offset, prog):
    p = PROFILES[site_id.split("-")[0]]
    fill, off, p95, hot, circ, wall = p["base"]
    d = p["drift"] * prog                      # prog: 0→1 随时间推进

    m = {
        "flame_fill_ratio_pct": round(max(3, _jitter(fill - d * 8)), 1),
        "centroid_offset_total_pct": round(max(0, _jitter(off + d * 9)), 1),
        "centroid_offset_x_pct": round(random.uniform(-18, 18), 1),
        "centroid_offset_y_pct": round(random.uniform(-14, 14), 1),
        "temp_index_mean": round(min(0.98, _jitter(p95 - 0.1)), 3),
        "temp_index_p95": round(min(0.99, _jitter(p95 + d * 0.10)), 3),
        "high_temp_area_pct": round(max(0.5, _jitter(hot + d * 10)), 1),
        "contour_circularity": round(max(0.08, min(0.99, _jitter(circ - d * 0.15))), 3),
        "hot_pixels_near_edge_pct": round(max(0, _jitter(2 + d * 5)), 1),
        "wall_hotspot_area_pct": round(max(0, _jitter(wall + d * 1.1)), 2),
        "whiteness_mean": round(_jitter(140), 1),
        "flame_detected": True,
    }
    spots = []
    if m["wall_hotspot_area_pct"] > 0.25:
        for _ in range(random.randint(1, 2)):
            spots.append({
                "位置": random.choice(["下右", "下中", "中左", "上右", "中右"]),
                "面积占比%": round(m["wall_hotspot_area_pct"] / 2, 2),
                "相对亮温": round(random.uniform(0.6, 0.85), 3),
            })
    m["wall_hotspots"] = spots
    m["wall_hotspot_count"] = len(spots)

    level, score, bad = _grade(m)
    ts = time.strftime("%Y-%m-%d %H:%M:%S",
                       time.localtime(time.time() - day_offset * 86400
                                      - random.randint(0, 40000)))
    ctx = {
        "level": level, "score": score,
        "headline": random.choice(HEADLINES[level]),
        "abnormal_count": bad,
        "contour_url": "", "heatmap_url": "",
    }
    return ts, ctx, m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--per-day", type=int, default=2, dest="per_day")
    ap.add_argument("--wipe", action="store_true")
    a = ap.parse_args()

    ledger.init()
    if a.wipe:
        import sqlite3
        with sqlite3.connect(ledger.DB_PATH) as c:
            c.execute("DELETE FROM runs")
        print("已清空台账")

    n = 0
    for d in range(a.days, 0, -1):
        prog = 1 - d / a.days
        for _ in range(a.per_day):
            s = random.choice(sites.SITES)
            ts, ctx, m = make_run(s["id"], d, prog)
            ledger.record(s["id"], ctx, m, "", ts=ts)
            n += 1

    print("写入 %d 条模拟记录，覆盖 %d 天、%d 个机组位置"
          % (n, a.days, len(sites.SITES)))
    for r in ledger.by_site(a.days + 1)[:6]:
        print("  %-22s %3d 次，高风险 %2d，均分 %s"
              % (sites.label_of(r["site"]), r["n"], r["high"], r["avg_score"]))


if __name__ == "__main__":
    main()
