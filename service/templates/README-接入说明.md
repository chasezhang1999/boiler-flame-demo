# main.py 里替换首页路由（其余不动）

拍照页拆成了手机版和网页版两套模板，按 UA 分发，`?view=` 可强制切换。

```python
import re
from fastapi import Request

_MOBILE_UA = re.compile(r"iphone|ipod|android.*mobile|windows phone|harmony", re.I)


@app.get("/", response_class=HTMLResponse)
def page_capture(request: Request, view: str = ""):
    """
    拍照页有两套版式：
      手机版 capture_mobile   —— 单列、大按钮、直接开后置摄像头
      网页版 capture_desktop  —— 左取像右结论、拖放与粘贴上传

    默认按 UA 猜，?view=mobile / ?view=desktop 可强制切换（页脚有链接）。
    """
    if view in ("mobile", "desktop"):
        mobile = view == "mobile"
    else:
        mobile = bool(_MOBILE_UA.search(request.headers.get("user-agent", "")))
    name = "capture_mobile.html.j2" if mobile else "capture_desktop.html.j2"
    return _pages.get_template(name).render(sites=sites.SITES)
```

## 模板清单

| 文件 | 说明 |
|---|---|
| `base.html.j2` | 壳：品牌（炉智检 + logo）、导航、共用 CSS、md2html / api / esc |
| `_capture_core.j2` | 拍照页共用脚本（两套版式的元素 id 一致，逻辑只此一份） |
| `capture_mobile.html.j2` | 手机版拍照页 |
| `capture_desktop.html.j2` | 网页版拍照页 |
| `history.html.j2` | 历史台账 |
| `chat.html.j2` | 历史问答 |
| `report.html.j2` | 报告页（未改动，沿用原文件） |

`logo.png` 放到 `service/assets/logo.png`，模板按 `/assets/logo.png` 引用。

---

# 机组筛选不生效的原因和补丁

前端选的机组在 **Dify 分支被丢掉了**（`DIFY_CHAT_KEY` 配了的时候）。

`main.py` 的 `/api/chat` 里，直连模型那条分支用的是 `_ledger_bundle(question, site_hint, days)`，
`site_hint` 就是前端传的 `site`，筛选是好的。但 Dify 分支只发了这些：

```python
json={"inputs": {"days": days}, "query": question, ...}
```

`site` 没进 `inputs`。chatflow 里的 HTTP 节点回调 `/api/ledger_bundle` 时也就带不上机组，
后端只能退回 `sites.resolve(question)` —— 从问题原文里猜。问题里没点名机组，
就变成全量台账，看起来"筛选没用"。

## 补丁 A：不动 DSL（推荐先上这个）

选了机组就把它写进 query，`sites.resolve()` 认得出这种写法：

```python
    if DIFY_CHAT_KEY and DIFY_BASE:
        # 机组要跟着 query 一起过去。chatflow 的 HTTP 节点只转发 query 和 days，
        # inputs 里加字段而不改 DSL 是没用的，所以把机位写进问题文本里。
        q = question if not site_id else "（仅限 %s）%s" % (site_label, question)
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(
                "%s/chat-messages" % DIFY_BASE,
                headers={"Authorization": "Bearer %s" % DIFY_CHAT_KEY},
                json={"inputs": {"days": days, "site": site_id},
                      "query": q, "response_mode": "blocking", "user": "web"},
            )
```

## 补丁 B：顺带把 DSL 改干净

`dify/chat.yml` 里"取台账"HTTP 节点的 body 加一个字段：

```json
{"question": "{{#sys.query#}}", "days": {{#start.days#}}, "site": "{{#start.site#}}"}
```

开始节点补一个 `site` 变量（文本，选填）。这样 `_ledger_bundle` 走的是 `site_hint` 那条路，
不再依赖从问题文本里猜。补丁 A 上了之后 B 可以慢慢做。

## 自检

```bash
# 直连分支（没配 DIFY_CHAT_KEY 时）——本来就是对的
curl -s -X POST localhost:18800/api/chat -H 'content-type: application/json' \
  -d '{"question":"最近风险怎么样","site":"u3-b","days":30}' | head -c 200
```

返回里的 `site` 字段应等于 `u3-b`；如果是空串，说明机位没传到，回答就是全量数据。
问答页右侧证据面板列的记录也应该只有该机位——那栏和回答取的是同一份台账，
可以直接当校验用。
