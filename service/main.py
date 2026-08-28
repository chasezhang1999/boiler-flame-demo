"""
锅炉火焰分析服务。

浏览器只跟这个服务打交道：Dify 的 API key 不能落到前端，所以分析请求由
这里代理转发给 Dify 工作流；对话问答直接查台账 + 调模型，不经 Dify。

  /api/*      前端用的接口
  /analyze    纯 CV，供 Dify 工作流的 HTTP 节点调用
  /report     渲染报告并落台账，同样供 Dify 调用
"""

import json
import os
import time
import uuid

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import charts, ledger, report as report_tpl, sites, vision

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATIC_DIR = os.path.join(ROOT, "static")
ASSETS_DIR = os.path.join(HERE, "assets")
os.makedirs(STATIC_DIR, exist_ok=True)

# 生成图片和报告页的对外地址前缀
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")

# Dify 工作流（分析链路）。key 只存在服务端。
DIFY_BASE = os.environ.get("DIFY_BASE", "").rstrip("/")
DIFY_KEY = os.environ.get("DIFY_API_KEY", "")

# 对话问答用的模型，直接调，不经 Dify
LLM_BASE = os.environ.get("LLM_BASE", "https://api.deepseek.com/v1").rstrip("/")
LLM_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-v4-flash-vision-exp")

app = FastAPI(title="Boiler Flame Service")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
if os.path.isdir(ASSETS_DIR):
    app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")

_pages = Environment(
    loader=FileSystemLoader(os.path.join(HERE, "templates")),
    autoescape=select_autoescape(["html"]),
)


@app.on_event("startup")
def _startup():
    ledger.init()


def _save(name: str, data: bytes) -> str:
    with open(os.path.join(STATIC_DIR, name), "wb") as f:
        f.write(data)
    return "%s/static/%s" % (BASE_URL, name)


# ---------------------------------------------------------------- CV 接口
# 这两个给 Dify 工作流的 HTTP 节点调用，不直接面向浏览器

@app.post("/analyze")
async def analyze(file: UploadFile = File(...)):
    t0 = time.time()
    bgr = vision.decode(await file.read())
    full, main = vision.flame_mask(bgr)
    temp = vision.temp_index(bgr)
    spots = vision.wall_hotspots(temp, full, main)

    tag = "%d_%s" % (int(time.time()), uuid.uuid4().hex[:6])
    import cv2
    ok1, buf1 = cv2.imencode(".jpg", vision.draw_contours(bgr, temp, main, spots),
                             [cv2.IMWRITE_JPEG_QUALITY, 88])
    ok2, buf2 = cv2.imencode(".jpg", vision.draw_heatmap(bgr, temp, main),
                             [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not (ok1 and ok2):
        raise HTTPException(500, "图片编码失败")

    return {
        "metrics": vision.compute_metrics(bgr, temp, main, full, spots),
        "contour_url": _save("%s_contour.jpg" % tag, buf1.tobytes()),
        "heatmap_url": _save("%s_heat.jpg" % tag, buf2.tobytes()),
        "elapsed_ms": int((time.time() - t0) * 1000),
    }


@app.post("/report")
async def make_report(
    assessment_text: str = Form(""),
    metrics_json: str = Form("{}"),
    contour_url: str = Form(""),
    heatmap_url: str = Form(""),
    shot_at: str = Form(""),
    site: str = Form(""),
):
    """
    渲染报告页并落台账。

    走 form-data 而不是 JSON：模型输出里带换行和引号，塞进 Dify 的 JSON body
    模板会把请求体撑坏，表单字段没这个问题。
    """
    try:
        metrics = json.loads(metrics_json) if metrics_json.strip() else {}
        if not isinstance(metrics, dict):
            metrics = {}
    except Exception:
        metrics = {}

    shot_at = shot_at or time.strftime("%Y-%m-%d %H:%M")
    s = sites.resolve(site)
    label = s["label"] if s else site

    name = "%d_%s_report.html" % (int(time.time()), uuid.uuid4().hex[:6])
    report_url = "%s/static/%s" % (BASE_URL, name)

    ctx = report_tpl.build_context(assessment_text, metrics, contour_url,
                                   heatmap_url, report_url, shot_at, label)
    with open(os.path.join(STATIC_DIR, name), "w", encoding="utf-8") as f:
        f.write(report_tpl.render_html(ctx))

    try:
        ledger.record(s["id"] if s else "", ctx, metrics, report_url)
    except Exception as e:                       # 台账坏了不能连累出报告
        print("[ledger] 写入失败：%s" % e)

    return {
        "report_url": report_url,
        "risk_level": ctx["level"],
        "markdown": report_tpl.render_markdown_from(ctx),
    }


# ---------------------------------------------------------------- 前端接口

@app.get("/api/sites")
def api_sites():
    return {"sites": sites.SITES}


@app.post("/api/analyze")
async def api_analyze(file: UploadFile = File(...), site: str = Form("")):
    """
    前端的分析入口：把图和机组转交给 Dify 工作流。

    浏览器不直接调 Dify，因为那需要把 API key 发到前端。
    """
    if not (DIFY_BASE and DIFY_KEY):
        raise HTTPException(503, "未配置 DIFY_BASE / DIFY_API_KEY")

    raw = await file.read()
    hdr = {"Authorization": "Bearer %s" % DIFY_KEY}
    async with httpx.AsyncClient(timeout=120) as c:
        up = await c.post(
            "%s/files/upload" % DIFY_BASE, headers=hdr,
            files={"file": (file.filename or "photo.jpg", raw,
                            file.content_type or "image/jpeg")},
            data={"user": "web"},
        )
        if up.status_code >= 300:
            raise HTTPException(502, "Dify 上传失败：%s" % up.text[:300])
        fid = up.json().get("id")

        run = await c.post(
            "%s/workflows/run" % DIFY_BASE, headers=hdr,
            json={
                "inputs": {
                    "photo": {"transfer_method": "local_file",
                              "upload_file_id": fid, "type": "image"},
                    "site": sites.label_of(site) if site else "",
                },
                "response_mode": "blocking",
                "user": "web",
            },
        )
    if run.status_code >= 300:
        raise HTTPException(502, "Dify 运行失败：%s" % run.text[:300])

    out = (run.json().get("data") or {}).get("outputs") or {}
    return {
        "summary": out.get("summary", ""),
        "report_url": out.get("report_url", ""),
        "risk_level": out.get("risk_level", "未知"),
    }


@app.get("/api/history")
def api_history(site: str = "", days: int = 0, limit: int = Query(50, le=500)):
    rows = ledger.history(site, days, limit)
    for r in rows:
        r["site_label"] = sites.label_of(r["site"])
    return {"rows": rows, "count": len(rows)}


@app.get("/api/summary")
def api_summary(site: str = "", days: int = 30):
    d = ledger.summary(site, days)
    d["site_label"] = sites.label_of(site) if site else "全部机组"
    return d


@app.get("/api/by_site")
def api_by_site(days: int = 30):
    rows = ledger.by_site(days)
    for r in rows:
        r["label"] = sites.label_of(r["site"])
    return {"rows": rows}


@app.get("/api/chart")
def api_chart(kind: str = "line", site: str = "", metric: str = "temp_index_p95",
              days: int = 30):
    """趋势图。直接返回 SVG，前端 <img> 或 <object> 都能用。"""
    try:
        if kind == "levels":
            rows = [{"label": sites.label_of(r["site"]),
                     "高": r["high"], "中": 0, "低": max(r["n"] - r["high"], 0)}
                    for r in ledger.by_site(days)]
            svg = charts.level_bars(rows)
        else:
            pts = ledger.series(site, metric, days)
            meta = report_tpl.METRIC_BY_KEY.get(metric)
            svg = charts.line_chart(
                pts,
                label="%s · %s" % (sites.label_of(site) if site else "全部机组",
                                   meta["label"] if meta else metric),
                unit=meta["unit"] if meta else "",
                band=meta["band"] if meta else None,
            )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return Response(svg, media_type="image/svg+xml")


@app.get("/api/ledger.csv")
def api_csv(site: str = "", days: int = 0):
    import csv
    import io as _io
    rows = ledger.history(site, days, 5000)
    buf = _io.StringIO()
    w = csv.writer(buf)
    w.writerow(["时间", "机组/位置", "风险等级", "评分", "结论",
                "超限项", "贴壁亮斑", "报告链接"])
    for r in rows:
        w.writerow([r["ts"], sites.label_of(r["site"]), r["risk_level"],
                    r["score"], r["headline"], r["abnormal_count"],
                    r["wall_hotspot_count"], r["report_url"]])
    # Excel 打开 UTF-8 CSV 需要 BOM，否则中文全是乱码
    return Response("﻿" + buf.getvalue(),
                    media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             'attachment; filename="ledger.csv"'})


SYS_PROMPT = """你是电站锅炉运行分析助手，负责回答关于历史巡检记录的提问。

只依据给你的台账数据作答。数据里没有的，直说「台账里没有这项记录」，
不要推测，也不要编造机组、时间或数值。

回答要求：
- 先给结论，再给依据，引用具体数字和日期
- 涉及趋势时说明方向和幅度，不要只说「有变化」
- 提到风险等级用「高 / 中 / 低」，跟台账口径一致
- 简明，不用寒暄，不要重复用户的问题
"""


@app.post("/api/chat")
async def api_chat(payload: dict):
    """
    历史问答。先按提问里的机组把台账取出来，连同问题一起交给模型。

    没有做 function calling —— 取数逻辑就三种（某机组汇总、横向对比、
    最近明细），直接全给模型比让它自己决定调什么工具更稳，也更省一轮往返。
    """
    if not LLM_KEY:
        raise HTTPException(503, "未配置 LLM_API_KEY")

    question = str(payload.get("question", "")).strip()
    if not question:
        raise HTTPException(400, "问题不能为空")
    days = int(payload.get("days") or 30)

    # 提问里点名了哪个机组就只查它，没点名就给全局对比
    site = sites.resolve(payload.get("site") or "") or sites.resolve(question)
    site_id = site["id"] if site else ""

    data = {
        "查询范围": "最近 %d 天" % days,
        "机组": site["label"] if site else "全部机组",
        "汇总": ledger.summary(site_id, days),
        "各机组对比": ledger.by_site(days)[:12],
        "最近记录": ledger.history(site_id, days, 12),
    }
    # 把内部 id 换成中文名再交给模型。留着 id 它会直接写进回答里，
    # 用户看到的就成了「u4-out 高风险 10 次」。
    for r in data["各机组对比"]:
        r["机组"] = sites.label_of(r.pop("site", ""))
    for r in data["最近记录"]:
        r["机组"] = sites.label_of(r.pop("site", ""))
        r.pop("id", None)

    body = {
        "model": LLM_MODEL,
        "temperature": 0.3,
        "messages": [
            {"role": "system", "content": SYS_PROMPT},
            {"role": "user", "content": "台账数据：\n%s\n\n问题：%s" % (
                json.dumps(data, ensure_ascii=False, indent=2, default=str),
                question)},
        ],
    }
    async with httpx.AsyncClient(timeout=90) as c:
        r = await c.post("%s/chat/completions" % LLM_BASE, json=body,
                         headers={"Authorization": "Bearer %s" % LLM_KEY})
    if r.status_code >= 300:
        raise HTTPException(502, "模型调用失败：%s" % r.text[:300])

    answer = r.json()["choices"][0]["message"]["content"]
    answer = report_tpl.strip_think(answer)

    # 问到某个机组时附一张趋势图，省得再让用户自己点
    chart = ""
    if site_id and ledger.series(site_id, "temp_index_p95", days):
        chart = "/api/chart?site=%s&metric=temp_index_p95&days=%d" % (site_id, days)

    return {"answer": answer, "site": site_id,
            "site_label": site["label"] if site else "全部机组",
            "chart_url": chart, "days": days}


@app.get("/health")
def health():
    return {"ok": True, "sites": len(sites.SITES)}


# ---------------------------------------------------------------- 页面

@app.get("/", response_class=HTMLResponse)
def page_capture():
    return _pages.get_template("capture.html.j2").render(sites=sites.SITES)


@app.get("/history", response_class=HTMLResponse)
def page_history():
    return _pages.get_template("history.html.j2").render(sites=sites.SITES)


@app.get("/chat", response_class=HTMLResponse)
def page_chat():
    return _pages.get_template("chat.html.j2").render(sites=sites.SITES)
