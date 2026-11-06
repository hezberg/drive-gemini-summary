# Google Drive「问问 Gemini / 摘要」逆向报告（终版：已完全跑通）

目标：文档链接 + 有权限的 Google 账号登录态 → 不开网页，纯 HTTP 拿到 Gemini 摘要。
**状态：✅ 复放成功（HTTP 200，摘要流完整返回）**，可用脚本见 `drive_gemini_summary.py`。

## 〇、最终协议（实测 200 验证）

```
POST https://appsgenaiserver-pa.clients6.google.com/v1/genai/streamGenerate
     ?key=AIzaSyDVQw45DwoYh632gvsP5vPDqEKvb-Ywnb8          ← 公开浏览器key
Content-Type:        application/json+protobuf; charset=UTF-8
Authorization:       SAPISIDHASH <unix秒>_<sha1("<unix秒> <SAPISID值> https://drive.google.com")>
Cookie:              <完整登录 Cookie：SID/HSID/SSID/SAPISID/__Secure-1PSID/...>
（禁止带 origin / x-origin 头 —— 会触发 400 "Origin doesn't match Host for XD3"）

Body: JSON 数组编码的 protobuf（apps_intelligence.gen_ai.StreamGenerateRequest），
      含 文件ID+mime、提示词、locale、时区、会话UUID、客户端构建号 goog_1178929964
Resp: application/json+protobuf 流式分片，摘要以 Markdown 文本嵌套在数组中
```

- 服务全名（401 报错亲口承认）：`apps_intelligence.gen_ai.GenAiService.StreamGenerate`
  @ `appsgenaiserver-pa.googleapis.com`
- 姊妹端点：`/v1/genai/quotaSummary`（配额）、`/v1/genai/listGems`、`/v1/genai/generate`（非流式）
- 摘要按 (用户,文件) 服务端持久化：同一文件第二次会命中缓存，首屏「摘要」卡片即来自缓存会话

## 逆向对象

## 一、已验证的完整架构

```
┌─ drive.google.com/file/d/{id}/view ────────────────────────────┐
│ base-js: gstatic.com/_/apps-fileview/_/js/...m=v,wb  (匿名可得) │
│                                                                  │
│ 匿名可用管线:                                                     │
│   GET /viewer/meta?id={签名id}        → 页数/尺寸                │
│   GET /viewer/img?id=..&page=N        → 每页位图                 │
│   GET /viewer/presspage?id=..&page=N  → 每页文本层 JSON(带坐标)   │
│   GET /uc?export=download&id={id}     → 完整 PDF 匿名下载 ✅      │
│                                                                  │
│ Gemini 面板(sidekick):                                            │
│   组件: com.google.apps.elements.xplat.sidekick (跨 Workspace)   │
│   打开方式: postMessage odp.OpenSidekickRequest                  │
│   挂载点: #viewer-sidekick-button                                 │
│   代码加载: 仅对有 AI 权益的登录用户下发独立 JS chunk(匿名不下发)   │
└──────────────────────────────────────────────────────────────────┘
                    │
                    ▼
┌─ GenAI 后端 ────────────────────────────────────────────────────┐
│ host:  appsgenaiserver-pa.clients6.google.com                    │
│ key:   AIzaSyDVQw45DwoYh632gvsP5vPDqEKvb-Ywnb8 (公开浏览器key)    │
│ 会话令牌: <anonymous-session-token>          │
│         (viewerData.configJson 随页面下发, 可能绑定会话)           │
│ 路径前缀: /v1/genai/  (来源: r/uBlockOrigin 屏蔽帖)               │
│ 协议: apps_intelligence.gen_ai.* protobuf (jspb 二进制)           │
│ 代表消息: ListChatMessagesRequest, StructuredDriveResource-      │
│          Reference, StructuredDocument, Card, Attribution,       │
│          ExecutionPlan, QuotaUpsellRecommendationsResponse       │
└──────────────────────────────────────────────────────────────────┘
```

配置原文（viewerData.configJson 内嵌数组，实测提取）:
```json
[0,"AIzaSyDVQw45DwoYh632gvsP5vPDqEKvb-Ywnb8","appsgenaiserver-pa.clients6.google.com",0,0,
 "AIzaSyDVQw45DwoYh632gvsP5vPDqEKvb-Ywnb8",null,0,0,0,0,null,0,0,null,null,null,0,1,0,null,
 0,0,0,0,"<anonymous-session-token>"]
```

## 二、外部佐证

- Netskope 企业管控文档确认 Gmail/Drive 的 Ask Gemini 流量全部指向 `appsgenaiserver-pa.clients6.google.com`
- 1Hosts / v2fly / Karing 规则集将其归类为 Google Workspace Gemini 后端
- r/uBlockOrigin 帖子：屏蔽 `appsgenaiserver-pa.clients6.google.com/v1/genai/` 可使 Sheets 侧边栏 Gemini 聊天失效 → 路径前缀 `/v1/genai/`
- 未登录 + 无 AI 权益账号 → 不下发 sidekick chunk、无面板按钮（与用户观察一致：功能绑定账号 AI 权益/配额）

## 三、鉴权踩坑记录（浏览器内复放实验矩阵）

| 尝试 | 结果 | 结论 |
|---|---|---|
| 仅 Cookie + key（fetch credentials:include） | 401 CREDENTIALS_MISSING | Cookie 单独不够 |
| + SAPISIDHASH(drive) + x-origin: drive | 400 "Origin doesn't match Host for XD3" | x-origin 头触发三方跨域拦截 |
| + SAPISIDHASH(clients6) 无 x-origin | 401 | hash 必须用页面 origin(drive) |
| **SAPISIDHASH(drive)，不带 x-origin（浏览器内）** | **200 ✅** | 正解：浏览器自动 Origin 头即可 |
| Node + 部分 Cookie（无 HSID/SSID） | 401 invalid credentials | 脚本需完整 Cookie（含 HttpOnly） |

客户端 JS 佐证：`com.google.apps.elements.xplat.network.http.web.CorsFpaDataOverHttpClient`
（FPA=First-Party Auth）+ `OAuthToken`，401 时检查 `www-authentication: SAPISIDHASH` 重试。

## 四、脚本使用（drive_gemini_summary.py）

```bash
# cookie 获取：已登录浏览器 F12 → Network → 过滤 appsgenaiserver → 点一条请求 →
# Request Headers → cookie 值整串复制
python3 drive_gemini_summary.py "https://drive.google.com/file/d/xxx/view" \
  --cookie 'SID=...; HSID=...; SSID=...; SAPISID=...; __Secure-1PSID=...' \
  --prompt '总结这份文件的要点。'
```

## 五、复放脚本模板（拿到抓包后填充）

```bash
# 变量从 Copy as cURL 的命令里抄
HOST="https://appsgenaiserver-pa.clients6.google.com"
METHOD="/v1/genai/XXXXX"          # ← 抓包得到
KEY="AIzaSyDVQw45DwoYh632gvsP5vPDqEKvb-Ywnb8"
COOKIE="SID=...; HSID=...; SSID=...; SAPISID=...; __Secure-1PAPISID=..."  # ← 抓包得到

curl -s "$HOST$METHOD?key=$KEY" \
  -H "content-type: application/json" \
  -H "origin: https://drive.google.com" \
  -H "referer: https://drive.google.com/" \
  -H "cookie: $COOKIE" \
  --data '{"fileId":"<TEST_FILE_ID_2>", ...}'   # ← 请求体结构抄抓包
```

要点：
- clients6 系网关的鉴权 = 浏览器 Cookie（SAPISID 家族）；若抓包显示 `authorization: SAPISIDHASH ...`
  则需按 `SHA1(unix时间 + " " + SAPISID + " " + origin)` 自算（aistudio2api 项目有现成实现）
- 摘要是服务端按 (用户, 文件) 生成并**持久化**的（proto 里有 ListChatMessagesRequest）：
  已生成过摘要的文件，复放列表/读取类请求即可拿缓存结果，不重复消耗配额
- proto 字段编号在 base.js 的 jspb 描述符数组里（`[0,Zp,-2,1,gq,...]`），需要时可继续解码

## 六、零逆向替代（当下可用，已实测）

```bash
curl -sL "https://drive.google.com/uc?export=download&id={FILE_ID}" -o f.pdf   # 匿名可下
# f.pdf 直接喂 Gemini/GLM 等支持 PDF 输入的模型 → 生成等同摘要
```

## 附：本目录产物

- view.html   匿名页面 HTML（含 viewerData 完整配置）
- base.js     主 bundle 4.8MB（sidekick/genai proto 定义所在）
- chunk1-9.js 匿名会话实际加载的懒加载 chunk
