"""
把一批真实火焰照片补录进台账。

跟 seed_ledger.py 的区别：那个是**编数据**，只为让图表有内容；
这个跑的是**真链路**——真实 CV 指标、真实模型判级、真实报告页，
只有「哪台机组、什么时候拍的」是模拟分散的（原始照片没带这些信息）。

不走 Dify 而是直连模型，是因为要控制每条记录的时间戳：Dify 工作流里
写死了不传 shot_at，补录的历史会全堆在导入那一刻，趋势图就废了。
CV 和提示词跟工作流里完全一致，结果等价。

在容器里跑（key 已在环境变量里）：
    docker exec flame-cv python tools/import_photos.py /app/inbox --days 45
"""

import argparse
import json
import os
import random
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service import sites   # noqa: E402

BASE = os.environ.get("SELF_BASE", "http://127.0.0.1:8000")
LLM_BASE = os.environ.get("LLM_BASE", "https://api.deepseek.com/v1").rstrip("/")
LLM_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-v4-flash-vision-exp")

PROMPT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "dify", "prompts", "risk_assessment.md")


def load_prompt():
    """从 prompts 文件里抠出 ```text 代码块，保证跟工作流用的是同一份。"""
    txt = open(PROMPT_PATH, encoding="utf-8").read()
    i = txt.find("```text")
    j = txt.find("```", i + 7)
    if i == -1 or j == -1:
        raise SystemExit("提示词文件里没找到 ```text 代码块：%s" % PROMPT_PATH)
    return txt[i + 7:j].strip()


def judge(client, img_b64, metrics, prompt):
    body = {
        "model": LLM_MODEL,
        "temperature": 0.3,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": [
                {"type": "text", "text":
                    "这是炉膛火焰照片。图像分析程序算出的量化指标如下：\n\n%s"
                    % json.dumps(metrics, ensure_ascii=False, indent=2)},
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64,%s" % img_b64,
                               "detail": "high"}},
            ]},
        ],
    }
    r = client.post("%s/chat/completions" % LLM_BASE, json=body,
                    headers={"Authorization": "Bearer %s" % LLM_KEY}, timeout=120)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir", help="照片目录")
    ap.add_argument("--days", type=int, default=45, help="把时间打散在最近多少天内")
    ap.add_argument("--dry", action="store_true", help="只跑 CV 不调模型不落库")
    a = ap.parse_args()

    if not LLM_KEY and not a.dry:
        raise SystemExit("没有 LLM_API_KEY")

    files = sorted(f for f in os.listdir(a.dir)
                   if f.lower().endswith((".png", ".jpg", ".jpeg")))
    if not files:
        raise SystemExit("目录里没有图片：%s" % a.dir)

    prompt = load_prompt()
    # 机组随机但尽量铺开：先打乱全部位置，轮着用，保证 16 个位置都有数据
    pool = []
    now = time.time()
    ok = fail = 0

    import base64
    with httpx.Client() as c:
        for n, fn in enumerate(files, 1):
            path = os.path.join(a.dir, fn)
            try:
                if not pool:
                    pool = sites.SITES[:]
                    random.shuffle(pool)
                site = pool.pop()

                # 时间打散：在 days 天里随机挑一天，配一个白天的时刻
                d = random.uniform(0.5, a.days)
                t = now - d * 86400
                lt = time.localtime(t)
                shot_at = time.strftime("%Y-%m-%d ", lt) + "%02d:%02d:%02d" % (
                    random.randint(7, 21), random.randint(0, 59), random.randint(0, 59))

                raw = open(path, "rb").read()
                r = c.post("%s/analyze" % BASE,
                           files={"file": (fn, raw, "image/png")}, timeout=120)
                r.raise_for_status()
                an = r.json()

                if a.dry:
                    m = an["metrics"]
                    print("  [%2d/%d] %-18s 充满度%5.1f%% 偏斜%5.1f%% 亮斑%d"
                          % (n, len(files), fn, m["flame_fill_ratio_pct"],
                             m["centroid_offset_total_pct"], m["wall_hotspot_count"]))
                    ok += 1
                    continue

                text = judge(c, base64.b64encode(raw).decode(), an["metrics"], prompt)

                rp = c.post("%s/report" % BASE, timeout=60, data={
                    "assessment_text": text,
                    "metrics_json": json.dumps(an["metrics"], ensure_ascii=False),
                    "contour_url": an["contour_url"],
                    "heatmap_url": an["heatmap_url"],
                    "shot_at": shot_at,
                    "site": site["id"],
                })
                rp.raise_for_status()
                lv = rp.json()["risk_level"]
                print("  [%2d/%d] %-18s %-22s %s  %s"
                      % (n, len(files), fn, site["label"], shot_at[:16], lv))
                ok += 1
            except Exception as e:
                print("  [%2d/%d] %-18s 失败：%s" % (n, len(files), fn,
                                                    str(e)[:100]))
                fail += 1

    print("\n成功 %d，失败 %d" % (ok, fail))


if __name__ == "__main__":
    main()
