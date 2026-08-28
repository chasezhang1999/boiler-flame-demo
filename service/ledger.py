"""
台账：每次分析落一行，供历史查询、趋势图和对话问答使用。

用 SQLite 而不是文件堆：要按机组和时间range 查、要算均值和分布，
SQL 一条搞定，自己拿 JSON 文件拼会很快失控。

写入时机是 /report —— 只有那一步同时拿得到量化指标和模型判级。
"""

import json
import os
import sqlite3
import threading
import time

DB_PATH = os.environ.get(
    "LEDGER_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "data", "ledger.db"),
)

# 常画趋势的几项拍平成独立字段，查询直接走 SQL 和索引，
# 不用逐行解 metrics_json。其余指标仍整包留在 metrics_json 里。
FLAT = [
    "flame_fill_ratio_pct", "centroid_offset_total_pct", "temp_index_p95",
    "high_temp_area_pct", "contour_circularity", "wall_hotspot_area_pct",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id                        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                        TEXT    NOT NULL,
  site                      TEXT    NOT NULL,
  risk_level                TEXT,
  score                     INTEGER,
  headline                  TEXT,
  abnormal_count            INTEGER DEFAULT 0,
  wall_hotspot_count        INTEGER DEFAULT 0,
  flame_fill_ratio_pct      REAL,
  centroid_offset_total_pct REAL,
  temp_index_p95            REAL,
  high_temp_area_pct        REAL,
  contour_circularity       REAL,
  wall_hotspot_area_pct     REAL,
  metrics_json              TEXT,
  contour_url               TEXT,
  heatmap_url               TEXT,
  report_url                TEXT
);
CREATE INDEX IF NOT EXISTS idx_site_ts ON runs(site, ts DESC);
CREATE INDEX IF NOT EXISTS idx_ts      ON runs(ts DESC);
"""

_lock = threading.Lock()


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _lock, _conn() as c:
        c.executescript(_SCHEMA)


def record(site: str, ctx: dict, metrics: dict, report_url: str, ts: str = "") -> int:
    """把一次分析写进台账。ctx 是 report.build_context() 的产物。"""
    row = {
        "ts": ts or time.strftime("%Y-%m-%d %H:%M:%S"),
        "site": site or "",
        "risk_level": ctx.get("level", "未知"),
        "score": ctx.get("score"),
        "headline": ctx.get("headline", ""),
        "abnormal_count": ctx.get("abnormal_count", 0),
        "wall_hotspot_count": metrics.get("wall_hotspot_count", 0),
        "metrics_json": json.dumps(metrics, ensure_ascii=False),
        "contour_url": ctx.get("contour_url", ""),
        "heatmap_url": ctx.get("heatmap_url", ""),
        "report_url": report_url,
    }
    for k in FLAT:
        row[k] = metrics.get(k)

    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    with _lock, _conn() as c:
        cur = c.execute("INSERT INTO runs (%s) VALUES (%s)" % (cols, marks),
                        list(row.values()))
        return cur.lastrowid


def _where(site: str = "", days: int = 0):
    sql, args = [], []
    if site:
        sql.append("site = ?")
        args.append(site)
    if days:
        # SQLite 的 now 恒为 UTC，而 ts 存的是本地时间（容器 TZ=Asia/Shanghai）。
        # 不加 localtime 的话「最近 N 天」会偏一个时区的量。
        sql.append("ts >= datetime('now', 'localtime', ?)")
        args.append("-%d days" % int(days))
    return (" WHERE " + " AND ".join(sql)) if sql else "", args


def history(site: str = "", days: int = 0, limit: int = 50) -> list:
    w, args = _where(site, days)
    with _conn() as c:
        rows = c.execute(
            "SELECT id, ts, site, risk_level, score, headline, abnormal_count,"
            " wall_hotspot_count, report_url, contour_url, heatmap_url"
            " FROM runs%s ORDER BY ts DESC LIMIT ?" % w, args + [int(limit)]
        ).fetchall()
    return [dict(r) for r in rows]


def series(site: str, metric: str, days: int = 30) -> list:
    """
    某指标的时间序列，供趋势图用。

    metric 会被拼进 SQL，所以必须先对着白名单校验 —— 它来自 HTTP 参数，
    直接拼是注入。
    """
    if metric not in FLAT:
        raise ValueError("不支持的指标：%s（可用：%s）" % (metric, ", ".join(FLAT)))

    conds, args = ["%s IS NOT NULL" % metric], []
    if site:
        conds.append("site = ?")
        args.append(site)
    if days:
        conds.append("ts >= datetime('now', 'localtime', ?)")
        args.append("-%d days" % int(days))

    sql = ("SELECT ts, %s AS v FROM runs WHERE %s ORDER BY ts ASC"
           % (metric, " AND ".join(conds)))
    with _conn() as c:
        rows = c.execute(sql, args).fetchall()
    return [{"ts": r["ts"], "v": r["v"]} for r in rows]


def summary(site: str = "", days: int = 30) -> dict:
    """次数、风险等级分布、关键指标均值、最近一次 —— 对话问答的取数入口。"""
    w, args = _where(site, days)
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) n FROM runs" + w, args).fetchone()["n"]
        levels = {r["risk_level"]: r["n"] for r in c.execute(
            "SELECT risk_level, COUNT(*) n FROM runs%s GROUP BY risk_level" % w, args)}
        avg = dict(c.execute(
            "SELECT %s FROM runs%s" % (
                ", ".join("ROUND(AVG(%s),3) AS %s" % (k, k) for k in FLAT), w),
            args).fetchone()) if total else {k: None for k in FLAT}
        latest = c.execute(
            "SELECT ts, site, risk_level, score, headline, report_url"
            " FROM runs%s ORDER BY ts DESC LIMIT 1" % w, args).fetchone()
        worst = c.execute(
            "SELECT ts, site, risk_level, score, headline, report_url FROM runs%s"
            " ORDER BY CASE risk_level WHEN '高' THEN 3 WHEN '中' THEN 2"
            " WHEN '低' THEN 1 ELSE 0 END DESC, score DESC LIMIT 1" % w,
            args).fetchone()
    return {
        "site": site, "days": days, "total": total,
        "levels": {k: levels.get(k, 0) for k in ("高", "中", "低")},
        "avg": avg,
        "latest": dict(latest) if latest else None,
        "worst": dict(worst) if worst else None,
    }


def by_site(days: int = 30) -> list:
    """各机组横向对比：次数、高风险占比、平均分。"""
    w, args = _where("", days)
    with _conn() as c:
        rows = c.execute(
            "SELECT site, COUNT(*) n,"
            " SUM(CASE WHEN risk_level='高' THEN 1 ELSE 0 END) high,"
            " ROUND(AVG(score),1) avg_score,"
            " ROUND(AVG(wall_hotspot_area_pct),2) avg_hotspot"
            " FROM runs%s GROUP BY site ORDER BY high DESC, n DESC" % w, args
        ).fetchall()
    return [dict(r) for r in rows]


def get(run_id: int):
    with _conn() as c:
        r = c.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return dict(r) if r else None


def delete(run_id: int) -> dict:
    """
    删一条记录，并把它生成的报告页和两张图一并删掉。

    这些文件只能从台账记录点进去，记录没了就是孤儿，留着白占地方。
    返回被删记录，调用方据此清理文件；找不到返回 None。
    """
    row = get(run_id)
    if not row:
        return None
    with _lock, _conn() as c:
        c.execute("DELETE FROM runs WHERE id = ?", (run_id,))
    return row
