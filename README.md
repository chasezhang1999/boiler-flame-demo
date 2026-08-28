# 锅炉火焰分析 Demo

拍一张炉膛火焰照片，输出**火焰轮廓图**、**相对亮温热力图**和**结焦风险评估报告**；
每次分析进台账，可按机组查趋势、可对话问询历史。

## 能做什么

| | |
|---|---|
| 拍照分析 | 手机对着看火孔拍一张 → 约 20 秒出报告；手机版 / 网页版按 UA 自动分发 |
| 图像分析 | 火焰分割、三级等值轮廓、相对亮温伪彩、11 项量化指标 |
| 风险判级 | 视觉模型结合图像与指标，给出高/中/低、判据和核查建议 |
| 历史台账 | 按机组和时间查记录、看趋势图、导出 CSV |
| 智能问答 | 「3号机组最近怎么样」这类问题，依据台账作答；可按整台机组或具体机位筛选 |

## 边界

> - 热力图是**未标定的相对亮温**，不是摄氏度。普通可见光相机推不出标定温度，
>   要定量得上红外热像或做黑体炉标定。
> - 指标的参考区间是**经验值**，未经历史样本标定。
> - 风险等级是模型推断，**不能作为运行调整依据**。
>
> 这是技术演示，不是生产系统。

## 架构

```
浏览器（拍照分析 / 历史台账 / 智能问答）
     │  只跟后端说话——Dify 和模型的 key 不能落到前端
     ▼
后端 (FastAPI)
 ├ /api/analyze  代理转发 Dify 工作流 → 落台账 → 返回报告
 ├ /api/chat     转发 Dify chatflow（未配 key 时直连模型兜底）
 ├ /api/history  台账明细 / 汇总 / CSV
 └ /api/chart    趋势图（服务端手排 SVG）
     │
     ├──→ Dify 分析工作流 ──→ 回调后端 /analyze 和 /report
     ├──→ Dify 问答 chatflow ──→ 回调后端 /api/ledger_bundle
     └──→ SQLite 台账
```

**为什么图不交给大模型画**：视觉模型输出的是文字，画不出图，也不保证吐得准坐标，
且对输入图有分辨率上限，定位小尺度亮斑不可靠。所以像素层面的活（分割、轮廓、
热力映射、量化指标）由 OpenCV 用确定性算法完成，模型只在「图像 + 指标」基础上做研判。
同一张图跑两次，指标完全一致，判级也稳定得多。

---

# 实施方案

从零到能演示，约 40 分钟。

## 0. 准备

| 项 | 要求 |
|---|---|
| 服务器 | Linux + Docker，**内存 ≥ 8G**（Dify 自己要 4~6G），磁盘 ≥ 20G |
| 域名 | 两个二级域名，分别给 Dify 和本服务；没有的话用 IP + 端口也能跑 |
| 模型 | 一个支持视觉的模型 API key，本项目用 DeepSeek |

## 1. 安装 Dify

```bash
git clone https://github.com/langgenius/dify.git --depth 1
cd dify/docker
cp .env.example .env
```

**如果这台机器 80/443 已被占用**（装了宝塔、1Panel 等），必须改端口，否则起不来：

```bash
sed -i "s/^EXPOSE_NGINX_PORT=.*/EXPOSE_NGINX_PORT=18080/" .env
sed -i "s/^EXPOSE_NGINX_SSL_PORT=.*/EXPOSE_NGINX_SSL_PORT=18443/" .env
docker compose up -d
```

打开 `http://<服务器>:18080/install` 设管理员账号。

## 2. 部署后端

```bash
git clone <本仓库> flame-demo && cd flame-demo
cp .env.example .env
```

编辑 `.env`。`DIFY_API_KEY` 先留空，第 3 步拿到再填：

```ini
BASE_URL=https://flame.example.com     # 本服务对外地址，报告里的图片链接用它
DIFY_BASE=http://docker-nginx-1/v1     # 同机部署走容器网络，不绕公网
DIFY_API_KEY=
LLM_BASE=https://api.deepseek.com/v1
LLM_API_KEY=sk-xxxx
LLM_MODEL=deepseek-v4-flash-vision-exp
```

`docker-compose.yml` 里的 `networks.dify.name` 要和 Dify 的 compose 网络一致，确认一下：

```bash
docker inspect docker-nginx-1 --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}'
```

启动并自检：

```bash
docker compose up -d --build
curl http://127.0.0.1:18800/health
```

应返回 `{"ok":true,"sites":16}`。

## 3. 配置 Dify

### 3.1 加模型

**不能用 DeepSeek 官方插件**——它是 predefined-model 模式，预置型号都没有 vision 能力，
也不支持手动添加自定义模型。走 **OpenAI-API-compatible**（DeepSeek 接口本来就是 OpenAI 格式）。

设置 → 模型供应商 → 安装 `OpenAI-API-compatible` → 添加模型：

| 字段 | 值 |
|---|---|
| 模型类型 | LLM |
| 模型名称 | `deepseek-v4-flash-vision-exp`（必须一字不差，DSL 按名字引用）|
| API Key | 你的 key |
| API endpoint URL | `https://api.deepseek.com/v1` |
| 模型上下文长度 | `1000000` |
| **是否支持 Vision** | **是** |

> ⚠️ **「是否支持 Vision」漏开是静默失败**：Dify 会把图片丢掉只发文本，且不报错——
> 节点照样绿、工作流照样成功、LLM 节点的 `vision.enabled` 也照样是 `true`。
> 判级结果看起来正常，其实模型一张图都没看到。**这是最容易踩的坑。**

### 3.2 导入工作流

工作室 → 创建应用 → 导入 DSL → 选 `dify/workflow.yml`：

```
开始(photo 图片 + site 下拉)
  → 图像校验(LLM+视觉)  → 是火焰照片？
        ├ 是 → CV图像分析(HTTP) → 解析结果(代码) → 结焦风险判级(LLM+视觉)
        │      → 渲染报告(HTTP) → 汇总输出(代码) → 结束
        └ 否 → 拒绝并说明(代码) → 结束（未分析）
```

**图像校验节点**先判断上传的是不是炉膛内部火焰照片。不加这一步的话，传张风景照
也会走完全流程，最后出一份一本正经的结焦报告——演示时被人随手传张无关图就穿帮。
它只输出 `FLAME` / `OTHER` 一个词，分支节点据此走向。否分支不落台账。

导入后要改三处：

1. **两个 HTTP 节点的地址**改成你的后端**公网地址**：`https://<你的域名>/analyze` 和 `/report`。
   不能填容器名或内网 IP——Dify 的 HTTP 节点有 SSRF 防护，访问私有地址会被拦
2. **两个 LLM 节点的视觉开关**都指向 `开始节点 / photo`——不是默认的 `sys.files`，
   工作流应用的文件走自定义变量，指错了模型收不到图
3. 确认**提示词**是 `dify/prompts/risk_assessment.md` 里那版

然后发布应用。

### 3.3 拿 API key 回填

应用页 → 访问 API → 创建密钥，填回后端 `.env` 的 `DIFY_API_KEY`，重启：

```bash
docker compose up -d
```

### 3.4 导入问答 chatflow

再建一个应用，导入 `dify/chat.yml`（四个节点）：

```
开始 → 取台账(HTTP) → 依据台账作答(LLM) → 回复
```

机组识别放在后端做（`sites.resolve()` 能认出「3号机组B层」这类说法），
所以不需要参数提取节点——少一个 LLM 节点就少一处失败点。

导入后改 HTTP 节点地址为 `https://<你的域名>/api/ledger_bundle`（同样不能填内网地址），
发布，然后在「访问 API」页生成密钥填进后端 `.env` 的 `DIFY_CHAT_KEY`。

> 不填 `DIFY_CHAT_KEY` 也能用——问答会退回直连模型，答案一样，
> 只是工作流画布上看不到这条链路。演示要讲编排就填上。

### 3.5 机组清单

改 `service/sites.py`，然后同步进 DSL 的下拉选项（Dify 读不了接口，选项只能写死）：

```bash
python tools/sync_sites.py
```

## 4. 对外访问

两个域名反代到本地端口，配上 HTTPS：

| 域名 | 后端 |
|---|---|
| `dify.example.com` | `127.0.0.1:18080` |
| `flame.example.com` | `127.0.0.1:18800` |

后端只绑 `127.0.0.1`，不直接暴露端口。**手机演示必须上 HTTPS**——调用摄像头的
`capture` 属性在非安全上下文下会被浏览器拒绝。

## 5. 验证

```bash
curl -X POST https://flame.example.com/api/analyze -F "file=@sample.jpg" -F "site=u1-a"
```

返回里带 `report_url` 即为打通。再打开三个页面：`/`、`/history`、`/chat`。

台账为空时趋势图和问答没东西可看，演示前可灌模拟数据：

```bash
docker exec flame-cv python tools/seed_ledger.py --days 45 --per-day 3
```

> 灌进去的是**编造的数据**，仅用于让图表有内容。对外演示务必说明，
> 或者用 `--wipe` 清掉后拍真照片积累。

## 6. 踩过的坑

| 现象 | 原因 |
|---|---|
| 判级正常但模型没看图 | Vision 支持开关没开，见 3.1 |
| 模型收不到图 | LLM 节点视觉变量指向了 `sys.files`，应指向 `开始节点/photo` |
| 报告页白屏十几秒 | 字体走了 Google Fonts。本项目已自托管，用 `tools/fetch_fonts.py` 抓取 |
| 报告变成「未按 JSON 返回」 | 模型输出带 `<think>` 思考块且里面有花括号。已在 `parse_assessment()` 先剥离 |
| 代码节点报 `unexpected keyword argument` | Dify 把**所有已声明变量**当关键字参数传入，删代码要连变量一起删 |
| HTTP 节点请求体被撑坏 | 模型输出带换行和引号。`/report` 用 form-data 而非 JSON |
| Dify 起不来，端口冲突 | 80/443 被占，见第 1 步改端口 |
| HTTP 节点报 `blocked by SSRF protection` | Dify 默认禁止访问私有 IP，节点地址要填公网域名，不能填容器名 |
| 前端报 `Unexpected token '<'` | 后端出错时反代返回的是 HTML 错误页。真实原因看后端日志或直连 `127.0.0.1:18800` |
| 问答筛了机组仍按全量作答 | chatflow 只收得到 `sys.query` 和 `days`，机位要写进 query 前缀让后端 `resolve()` 认；且 `resolve()` 必须能从整句里认出机位 |

---

## 目录

```
service/          后端
  main.py         FastAPI 入口：路由与挂载
  vision.py       OpenCV：分割 / 轮廓 / 热力图 / 指标
  report.py       报告上下文准备
  ledger.py       台账（SQLite）
  charts.py       趋势图（手排 SVG，不引 matplotlib）
  sites.py        机组清单——唯一事实来源
  templates/      报告模板 + 页面
    capture_mobile / capture_desktop  拍照页两套版式，按 UA 分发，
                                      ?view=mobile|desktop 可强制切换
    _capture_core.j2                  两套版式共用的脚本（元素 id 一致）
  assets/fonts/   自托管字体
dify/
  workflow.yml    分析工作流 DSL（含火焰照片校验分支）
  chat.yml        智能问答 chatflow DSL
  prompts/        提示词，独立成文件便于评审
tools/
  import_photos.py 把真实照片批量补录进台账（真 CV + 真判级）
  build_workflow.py 从线上 graph 生成带校验分支的 DSL
  seed_ledger.py  灌模拟历史
  sync_sites.py   机组清单同步进 DSL
  fetch_fonts.py  抓取自托管字体
docs/             端到端流程图
data/             (gitignore) 台账，挂载卷，重建镜像不丢
```

改报告版式只改 `service/templates/*.j2`，容器重启即生效，不用动 Python。

## 还没做的

| 缺什么 | 影响 |
|---|---|
| 参数规则工具 | 蒸汽温度 / 压力的规则检查没有，只有图像这条线 |
| 「证据是否充分」分支 | 不会输出「信息不足」，缺输入也照样给结论 |
| 对话式追问 | 开始节点一次性收齐输入，不会追问补齐 |

`docs/端到端流程图.svg` 画的是**目标设计**，上面三项图里有、代码里没有，对外讲注意口径。
