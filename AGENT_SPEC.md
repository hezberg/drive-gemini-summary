# Drive Gemini 摘要工具 — Agent 开发规格书

> 交接文档：本文档面向将要开发集成工具的 AI agent。
> 协议已由前序会话**实测跑通（HTTP 200）**，无需重新逆向；直接按本文实现。
> 所有结论的原始证据存于本目录，最后附清单。

---

## 1. 任务定义

**输入**：Drive 文件链接（或文件 ID）+ 有 Gemini 权益的 Google 账号登录 Cookie + 可选 prompt
**输出**：该文件的 Gemini 摘要（Markdown 文本，与 Drive 网页「问问 Gemini」面板内容同源同质）
**硬约束**：纯 HTTP 实现，**不做浏览器自动化**（用户明确否决）；消耗的是用户自己的 Gemini 配额。

参考实现：本目录 `drive_gemini_summary.py`（单文件、纯标准库、已验证可用）。你要做的是把它工程化成集成工具。

---

## 2. 协议规格（已验证，照抄即可）

### 2.1 端点

```
POST https://appsgenaiserver-pa.clients6.google.com/v1/genai/streamGenerate?key=AIzaSyDVQw45DwoYh632gvsP5vPDqEKvb-Ywnb8
```

- `key` 是公开浏览器 key（Drive 页面 HTML 明文下发），**不是**密钥，不参与鉴权，缺失或错误会 404。
- 服务全名：`apps_intelligence.gen_ai.GenAiService.StreamGenerate`（401 报错信息里亲口承认）。
- 姊妹端点（可选集成）：
  - `POST /v1/genai/quotaSummary?key=<同key>`，body `[null,1,[19]]` → 返回剩余配额（JSON-protobuf）
  - `POST /v1/genai/listGems?key=<同key>` → 用户的 Gem 列表
  - `POST /v1/genai/generate` → 非流式版本（未实测）

### 2.2 请求头（三条铁律）

```
Content-Type:  application/json+protobuf; charset=UTF-8
Authorization: SAPISIDHASH <unix秒>_<sha1("<unix秒> <SAPISID值> https://drive.google.com")>
Cookie:        <完整登录 Cookie，含 HttpOnly 项>
User-Agent:    任意现代浏览器 UA
```

**铁律：**
1. **绝对不能发送 `origin` 或 `x-origin` 头** → 一带就 400 `Origin doesn't match Host for XD3`。
2. SAPISIDHASH 的 hash 输入第三段**必须是** `https://drive.google.com`（页面 origin），
   用 `https://clients6.google.com` 会 401。
3. Cookie 必须完整（见 §4）；`document.cookie` 只能拿到非 HttpOnly 的一半，不够。

SAPISIDHASH 生成（Python）：

```python
import hashlib, time
def sapisidhash(sapisid: str) -> str:
    t = int(time.time())
    return f"SAPISIDHASH {t}_{hashlib.sha1(f'{t} {sapisid} https://drive.google.com'.encode()).hexdigest()}"
```

时间戳按秒；过期窗口未精确测定，**每次请求现算现用**即可，不要缓存。

### 2.3 请求体

JSON 数组编码的 protobuf（`application/json+protobuf`：`null`=字段缺省，数字=varint/enum，数组下标=字段号-1）。
以下是**实测 200 的完整模板**，可直接参数化：

```json
[[15,null,
  [19,null,null,null,"goog_1178929964",[null,null,null,null,[0]],"1",null,
   [null,null,null,
    [[[null,null,null,null,null,null,null,
       [null,null,[null,null,null,["<FILE_ID>",null,"application/pdf"]]]]]]],
   null,null,[3,0],null,"zh-CN",
   null,null,null,null,null,1,
   null,null,null,null,null,null,null,null,"<SESSION_HEX32>",
   [],0,"<CONV_UUID>",["<MSG_UUID>"],
   null,"Asia/Shanghai",
   null,null,null,null,null,0,null,null,null,null,[1,1]],
  [null,null,"<PROMPT>"],
  null,[1,null,[[null,"1",1182]]],1],
 [1,null,1]]
```

字段语义（已知部分；未知字段**照抄默认值**，勿"优化"）：

| 片段 | 含义 | 稳定性 |
|---|---|---|
| `goog_1178929964` | 客户端构建号 | **易变**，随 Drive 前端发版更新（维护见 §6.1） |
| `"1"`（构建号后第 3 位） | 会话轮次：`0`=取建议提示词(starter tiles)，`1`=首次生成，`2+`=追问（需带历史） | 稳定 |
| `<FILE_ID>` + `"application/pdf"` | Drive 资源引用（文件 ID + MIME） | 稳定；其他 MIME 应同理 |
| `[3,0]` | 未知（疑似 surface 标记） | 照抄 |
| `"zh-CN"` / `"Asia/Shanghai"` | locale / 时区 | 可按需改 |
| `SESSION_HEX32` | 会话 ID：32 位大写十六进制 | 每会话随机生成 |
| `CONV_UUID` / `MSG_UUID` | 会话/消息 UUID（大写） | 每请求随机生成 |
| `<PROMPT>` | 提示词，如 `总结这份文件的要点。` | 自由文本 |
| `[1,null,[[null,"1",1182]]]` | 宿主上下文（Drive viewer surface 标记） | 照抄 |
| 外层 `[[15,...],[1,null,1]]` | 消息包装 | 照抄 |

**轮次说明**：只做"生成摘要"功能的话永远用 `"1"` + 新 UUID，无需管历史轮次。
带历史的追问格式见 `RECON.md` 的抓包样本（`[[1,<prompt>,...],[2,<历史回复>,...]]`）。

### 2.4 响应

- Content-Type: `application/json+protobuf; charset=UTF-8`；**单个 JSON 文档**（虽然接口名叫 stream，
  XHR 的最终 responseText 是完整数组，长度典型 1KB~1.5MB）。
- 结构：顶层是"流式消息"的数组，每条消息内文本片段嵌套在深层。
- **解析规则（已验证的实用算法）**：递归遍历整棵 JSON 树，按出现顺序收集所有
  长度 ≥6 的字符串，跳过"已见字符串的子串"（同一段文本会以 整段/分段/样式段 形式重复出现 2~3 次），
  拼接即得完整 Markdown。参考实现见 `drive_gemini_summary.py` 的 `extract_text()`。
- 首条消息开头会有若干 `[null,[N]]` 状态计数与建议卡片（starter tiles）；纯文本提取算法天然跳过它们。
- 若要精细解析（分块增量渲染），文本段的稳定位置模式为：
  `msg[7][0][0][0][0][0][0][0]`（首块）及后续块的 `msg[...][0]`，风险自负——实用算法已够用。

### 2.5 验证样例（回归测试用）

| 文件 | ID | 预期摘要开头 |
|---|---|---|
| sample research PDF A | `<TEST_FILE_ID_1>` | expected keyword from its summary |
| sample research PDF B | `<TEST_FILE_ID_2>` | expected keyword from its summary |

e2e 断言：HTTP 200 且提取文本含上述关键词之一。

---

## 3. 错误码对照表（诊断即查）

| 状态/报错 | 根因 | 处置 |
|---|---|---|
| 404 空响应 | 未带 Cookie 探测 / key 错 / 路径错 | 检查 key 与完整请求头 |
| 401 `API keys are not supported…Expected OAuth2` | 只带了 key，没带 Authorization | 加 SAPISIDHASH |
| 401 `Request had invalid authentication credentials…login cookie` | Cookie 不全（缺 HttpOnly 项）或 SAPISIDHASH 算错/过期 | 重新导出完整 Cookie；核对 hash 三段格式 |
| 400 `Origin doesn't match Host for XD3` | 请求带了 `origin`/`x-origin` 头 | **删掉这两个头** |
| 400 其他 bad request | 请求体结构错 / 构建号过期 / UUID 格式错 | 对照 §2.3 模板逐字段 diff；更新构建号 |
| 429 / quota | 配额耗尽 | 调 `quotaSummary` 展示余量；次日再试 |
| 响应 200 但文本为空 | prompt 被安全策略吞 / 文件无可提取文本 | 换 prompt 重试；确认文件是文本型 PDF |

---

## 4. Cookie 的获取与安全

### 4.1 需要哪些 Cookie

最小集（实测推断，多多益善）：`SID`、`HSID`、`SSID`、`SAPISID`、`__Secure-1PSID`、`__Secure-3PSID`、
`__Secure-1PAPISID`、`__Secure-3PAPISID`、`SIDCC`、`SEARCH_SAMESITE`。
**关键点**：`HSID/SSID/__Secure-*PSID` 是 HttpOnly，`document.cookie` 拿不到，脚本注入方式不可行。

### 4.2 引导用户获取（工具内建流程）

1. 已登录（有 AI Pro）的浏览器打开任意 Drive PDF；
2. F12 → Network → 过滤 `appsgenaiserver` → 点「问问 Gemini」触发一条请求；
3. Request Headers → `cookie:` 整串复制 → 存入 cookie 文件。
   （更顺手：右键请求 → Copy as cURL，把整条命令原样粘进去，工具自动抽 cookie。）

**Cookie 文件格式（CLI `drive_summarize.py` 已实现）**：JSON，路径任意（`--cookie-file` 传入，
默认 `~/.drive_summarize/cookies.json`）。文件里可以有任意多个键——工具递归定位含 `SAPISID`
的值，三种形态都认：

```json
{"openai": "sk-...", "google": "SID=...; SAPISID=...; HSID=..."}        // 值=整串
{"google": "curl '...' -H 'cookie: SID=...;'"}                          // 值=cURL 命令
{"google": {"SID": "...", "SAPISID": "...", "SSID": "..."}}             // 值=扁平映射
```

多账号候选：默认取第一个并打印所用键路径（`$.google` 风格），`--cookie-key <键名>` 指定。
环境变量覆盖链：`DRIVE_COOKIE`（整串值）> `DRIVE_COOKIE_FILE` > 配置文件。

### 4.3 安全要求（必须实现）

- Cookie = 完整账号凭据：落盘必须 `chmod 600`，日志中必须脱敏（只显前 12 字符）；
- 绝不打印 SAPISIDHASH 原值与 Cookie 到 stdout/stderr；
- 建议支持 `DRIVE_COOKIE_FILE` 指向加密/受限目录文件，避免进 shell history。

### 4.4 有效期

SAPISID/PSID 家族为长效 Cookie（月级）；失效症状 = 突发 401（§3）→ 提示用户重新导出。
**不要**自动尝试刷新/续期 Google 会话（风控敏感），让用户手动重导。

---

## 5. 工具架构建议

```
输入层    CLI: drive-gemini <link> [--prompt ...] [--cookie ...]
            或 长驻服务: POST /summarize {url, prompt}
              ↓
预处理    解析链接→FILE_ID；解析/加载 Cookie（支持从 Copy-as-cURL 文本抽取）
              ↓
可选预检  quotaSummary → 余量不足直接报错（省一次生成）
              ↓
请求构造  §2.2 + §2.3（UUID 每次新生成；SAPISIDHASH 现算）
              ↓
HTTP POST streamGenerate（超时 ≥120s；大文件响应可达 1.5MB+）
              ↓
解析      extract_text() 递归提取（§2.4）
              ↓
输出      Markdown 到 stdout / 文件；退出码：0 成功，2 鉴权失败，3 配额，4 协议变更(400)
```

工程要点：
- **退出码区分协议变更**：400 且非 XD3 文案时返回专用码，方便上层触发 §6 维护流程；
- **服务端缓存**：摘要按(用户,文件)持久化，重复请求同文件命中缓存，速度快且省配额——工具层不必自己建缓存，但可以把同一文件并发请求去重；
- **频率克制**：这是用户个人配额；串行 + 必要间隔，不做并发轰炸。

---

## 6. 维护规程（工具失效时照此排查）

### 6.1 构建号漂移（最可能的失效点）

- **症状**：长期 400 bad request，而 Cookie 刚导过、模板没动过。
- **定位**：`goog_1178929964` 过期。
- **更新方法 A（推荐，1 分钟）**：登录浏览器抓一次包（§4.2），从请求体第 8 个字段抄新构建号。
- **更新方法 B（无抓包条件）**：下载当前 Drive 前端 JS 搜索——
  ```
  1. GET https://drive.google.com/file/d/<任意文件>/view   （匿名即可）
  2. 提取 HTML 里 apps-fileview 的 base-js 与 chunk URL
  3. 登录态才下发 AI chunk —— 匿名 chunk 里搜不到构建号，此路仅能验证其他字段
  ```
  → 结论：**方法 A 是唯一可靠路径**，把构建号做成配置项而非硬编码。
- 顺带校验：key（`AIzaSyDV…`）与外层结构是否同批变化。

### 6.2 协议升级探测（月度或失效时）

1. 端点是否仍为 `/v1/genai/streamGenerate`（抓包确认）；
2. 请求体模板 diff：把新抓的请求体与 §2.3 模板逐下标对比，记录增量字段；
3. 响应结构变化：跑一次 e2e 样例（§2.5），`extract_text()` 仍能出文本即兼容。

### 6.3 监控清单（集成到工具的健康检查）

- [ ] e2e 样例文件可出摘要（两个都测）
- [ ] quotaSummary 返回 200
- [ ] Cookie 年龄 < 60 天（记录导出时间戳）
- [ ] 构建号年龄 < 90 天（记录更新时间戳）

### 6.4 已知不该做的事（前人踩坑，勿重复）

- ❌ 带 `origin`/`x-origin` 头（400 XD3）
- ❌ 用 `clients6.google.com` 做 hash origin（401）
- ❌ 只用 `document.cookie` 收集 Cookie（401）
- ❌ 匿名探测端点做健康检查（一律 404，会误报协议死了）
- ❌ 尝试绕过/伪造 Origin（浏览器内做不到，浏览器外不需要——不带头就行）
- ❌ 把此协议用于非本人账号（Cookie 即账号；本工具定位是个人自用配额）

---

## 7. 法务与风控边界

- 该接口是 Google 内部第一方接口（无公开 API），使用性质 = 用自己的账号、自己的配额、
  读自己有权限访问的文件，等同网页端手动操作；
- 保持人的速率（串行、按需），不做账号池、不做批量爬取——这既是 ToS 边界也是风控自保；
- 工具输出请保留「内容来自 Gemini，可能出错」类提示（响应元数据里也有此声明字段）。

---

## 8. 附录：Google Sheets 的 Gemini（2026-11-06 实测对比）

同一套后端，上下文不同。集成工具支持 Sheets 时按下表适配：

| 维度 | Drive PDF 查看器 | Sheets |
|---|---|---|
| 端点/鉴权/key | `appsgenaiserver-pa.clients6.google.com/v1/genai/*` + SAPISIDHASH | **完全相同**（实测 200） |
| 客户端构建号 | `goog_1178929964` | `goog_424115329`（**每个 App 独立**） |
| App/surface 枚举 | `[19,...]` | `[3,...]` |
| 资源引用 | `[fileId, null, "application/pdf"]` | `[fileId, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", 1, <选区JSON>, 0, "<revision如2dd_hbqH5216zQ>", ...]` |
| 选区上下文 | 无 | **有**：sheet gid、选中范围、单元格内值、视口尺寸 [w,h]（AI 能看到当前选中数据） |
| 消息类型 | `[61,...]`=建议卡片，`[15,...]`=生成 | 相同（实测 [61]，[15] 结构推断同型） |
| UI 入口 | 「问问 Gemini」按钮 | 同款按钮 + 专用 Gemini 菜单 13 项：分析数据/解决优化问题/生成图表/透视表/表格/图片/公式/总结文本/分类/情绪分析/生成文本/问其他问题 |

Sheets starter tiles 响应的建议是表格任务型（"创建图库视图""创建信息中心"等）。

**未竟事项（诚实记录）**：
- Sheets 的 `[15,...]` 生成调用未捕获成功——面板在页面重载后改走了跨源 gapi proxy iframe
  （`clients6.google.com/static/proxy.html`），脚本层 hook 不可见。补齐方法：登录态 DevTools →
  Network → 过滤 `streamGenerate` → 在 Sheets 面板发一条消息 → Copy as cURL。
- 「分析数据」菜单项点击后未见独立请求（面板未在本会话打开）；推断它 = sidekick + 预设 prompt，
  走同一端点，待 DevTools 验证。
- **PDF 与 Sheets 的请求体结构同源**：适配 Sheets 只需改 构建号 + surface 枚举 + 资源三元组，
  鉴权与端点零改动。

## 9. 本目录证据清单

| 文件 | 内容 |
|---|---|
| `AGENT_SPEC.md` | 本文档 |
| `drive_summarize.py` | **成品 CLI**（§5 架构的落地）：参数文件 `~/.drive_summarize/config.json` 自动生成；cookie 文件 = 任意路径 JSON（`--cookie-file`），多键自动定位含 SAPISID 的值（整串/cURL/扁平映射三形态），多候选 `--cookie-key` 指定；api_key/host 自动抓取并缓存（30 天）；构建号自动尝试+内置兜底；`--quota` `--out` `--selftest`；退出码 0/2/3/4/5 |
| `drive_gemini_summary.py` | 早期最小参考实现（单文件，标准库） |
| `RECON.md` | 完整逆向报告：架构、鉴权实验矩阵、原始结论 |
| `view.html` | 匿名页面 HTML(viewerData config: key, host, session token) |
| `base.js` / `ai_chunk1.js` | 前端 bundle：`apps_intelligence.gen_ai` proto 定义、`CorsFpaDataOverHttpClient` 客户端 |
| `chunk*.js` | 匿名会话的其他 chunk（参考） |

外部佐证：Netskope 企业管控文档（Ask Gemini 流量指向 appsgenaiserver-pa）、
r/uBlockOrigin（`/v1/genai/` 路径）、aistudio2api 项目（SAPISIDHASH 算法参考）。
