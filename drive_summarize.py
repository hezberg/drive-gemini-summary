#!/usr/bin/env python3
"""
Drive Gemini 摘要 CLI — 纯 HTTP 复放（协议规格见 AGENT_SPEC.md）

唯一需要人工提供：Cookie 文件（有 AI 权益的浏览器 DevTools 复制，详见 --help 或首次运行提示）。
其余全部自动：API key/端点 自动从文件页面抓取并缓存；SAPISIDHASH 现算；UUID 现生成。

用法:
  python3 drive_summarize.py <drive链接或文件ID>                 # 摘要到 stdout
  python3 drive_summarize.py <链接> --prompt "列出要点" --out a.md
  python3 drive_summarize.py <链接> --quota                     # 查 Gemini 配额
  python3 drive_summarize.py --selftest                        # 离线自检

参数文件（--config，默认 ~/.drive_summarize/config.json，首次运行自动生成模板）:
  cookie_file     cookie 文件路径（相对 config 所在目录）
  prompt          默认提示词
  locale/timezone 输出语言与时区
  api_key/host    自动抓取后回写缓存，一般不用动
  client_build    客户端构建号：自动尝试，抓不到时用内置值；失效(400)时手工更新（见 AGENT_SPEC §6.1）

Cookie 文件（--cookie-file 可传任意路径）。优先级（高→低）：
  1. --cookie-file 参数
  2. 环境变量 DRIVE_COOKIE_FILE（文件）/ DRIVE_COOKIE（整串值）
  3. 配置文件里的 cookie_file（相对 config 所在目录）
  JSON 格式，多键无所谓 —— 递归自动定位含 SAPISID 的那个值，支持三种形态：
    {"google": "SID=...; SAPISID=...; HSID=..."}          ← 值=整串 cookie
    {"google": "curl '...' -H 'cookie: SID=...;'"}        ← 值=Copy as cURL 命令
    {"google": {"SID": "...", "SAPISID": "...", ...}}     ← 值=扁平 cookie 映射
  多个账号候选时默认用第一个，--cookie-key <键名> 指定；运行时会打印用了哪个键。
退出码: 0 成功 | 2 鉴权失败 | 3 配额 | 4 协议/接口错误 | 5 配置缺失 | 6 网络异常
"""
import argparse, hashlib, json, os, re, ssl, sys, tempfile, time, urllib.request, uuid

# ---- 内置兜底值（自动抓取失败时使用；来源见 AGENT_SPEC） ----
DEFAULT_KEY = "AIzaSyDVQw45DwoYh632gvsP5vPDqEKvb-Ywnb8"
DEFAULT_HOST = "appsgenaiserver-pa.clients6.google.com"
DEFAULT_BUILD = "goog_1178929964"          # 会随 Drive 前端发版漂移 → config 可覆盖
ORIGIN = "https://drive.google.com"        # SAPISIDHASH hash 用，勿改
REFRESH_AFTER = 30 * 86400                 # key/host 缓存有效期（秒）

def die(code: int, msg: str):
    print(msg, file=sys.stderr); sys.exit(code)

# ---------------- 配置 ----------------
def load_config(path: str) -> dict:
    cfg = {"cookie_file": "cookies.json", "prompt": "总结这份文件的要点。",
           "locale": "zh-CN", "timezone": "Asia/Shanghai",
           "api_key": None, "host": None, "client_build": None,
           "_fetched_at": 0}
    if os.path.exists(path):
        try:
            cfg.update(json.load(open(path, encoding="utf-8")))
        except Exception as e:
            die(5, f"配置文件解析失败 {path}: {e}")
    else:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        save_config(path, cfg)      # 原子写 + chmod 600（首次创建同样受保护）
        print(f"已生成配置模板: {path}\n请把 cookie 存到 "
              f"{os.path.join(os.path.dirname(os.path.abspath(path)), cfg['cookie_file'])}"
              f"（JSON，多键随意，自动定位含 SAPISID 的值）", file=sys.stderr)
    return cfg

def save_config(path: str, cfg: dict):
    """原子写入（tmpfile + rename），防并发/中断留下半截 JSON；chmod 600（含 cookie 路径等信息）"""
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        os.unlink(tmp) if os.path.exists(tmp) else None
        raise

# ---------------- Cookie ----------------
def from_curl(text: str) -> str:
    """Copy as cURL 粘贴 → 抽 cookie 头；非 cURL 原样返回"""
    if not text.lstrip().startswith("curl "):
        return text.strip().rstrip(";")
    m = (re.search(r"(?:-H|--header)\s+[\"']cookie:\s*([^\"']+)[\"']", text, re.I)
         or re.search(r"(?:--cookie|-b)\s+[\"']([^\"']+)[\"']", text, re.I))
    if not m: die(5, "从 cURL 命令里没找到 cookie 头（支持 -H/--header 'cookie:'、--cookie、-b）")
    return m.group(1).strip().rstrip(";")

def find_cookie_values(node, path="$"):
    """递归收集 (路径, cookie串)。两类候选：含 SAPISID= 的字符串值；含 SAPISID 键的扁平映射。
    字符串形态优先于映射形态。"""
    strs, maps = [], []
    def walk(n, p):
        if isinstance(n, dict):
            if "SAPISID" in n and all(isinstance(v, str) for v in n.values()):
                maps.append((p, "; ".join(f"{k}={v}" for k, v in n.items())))
            for k, v in n.items():
                walk(v, f"{p}.{k}")
        elif isinstance(n, list):
            for i, v in enumerate(n):
                walk(v, f"{p}[{i}]")
        elif isinstance(n, str) and "SAPISID=" in n:
            strs.append((p, n))
    walk(node, path)
    return strs + maps

def load_cookie(cfg: dict, cfg_path: str, cookie_file: str = None, cookie_key: str = None) -> str:
    # 优先级：显式 --cookie-file > DRIVE_COOKIE_FILE > DRIVE_COOKIE（整串值）> 配置
    if cookie_file:
        cf = cookie_file
    elif os.environ.get("DRIVE_COOKIE_FILE"):
        cf = os.environ["DRIVE_COOKIE_FILE"]
    elif os.environ.get("DRIVE_COOKIE"):
        return from_curl(os.environ["DRIVE_COOKIE"])
    else:
        cf = os.path.join(os.path.dirname(os.path.abspath(cfg_path)), cfg["cookie_file"])
    if not os.path.exists(cf):
        die(5, f"缺少 cookie 文件 {cf}\nJSON 格式示例（多键随意，会自动定位含 SAPISID 的值）:\n"
               f'  {{"openai": "sk-...", "google": "SID=...; SAPISID=...; HSID=..."}}\n'
               f"获取: 已登录浏览器 F12 → Network → 过滤 appsgenaiserver → 点一条请求 → "
               f"Request Headers → cookie 整串（或 Copy as cURL 命令）填进任意键值")
    if (os.stat(cf).st_mode & 0o077) and os.name != "nt":
        print(f"[cookie] 警告: {cf} 对组/其他用户可读，建议 chmod 600 {cf}", file=sys.stderr)
    content = open(cf, encoding="utf-8").read().strip()
    try:
        data = json.loads(content)
    except Exception:
        print(f"[cookie] {cf} 不是 JSON，按纯文本 cookie 解析（建议改用 JSON）", file=sys.stderr)
        return from_curl(content)
    cands = find_cookie_values(data)
    if not cands:
        die(5, f"{cf} 里没找到候选：需要含 'SAPISID=' 的字符串值，或含 SAPISID 键的扁平映射")
    if cookie_key:
        sel = [c for c in cands if c[0] == cookie_key or c[0].rsplit(".", 1)[-1] == cookie_key]
        if not sel:
            die(5, f"--cookie-key {cookie_key} 未匹配。可用候选: {[p for p, _ in cands]}")
        cands = sel
    path, val = cands[0]
    if len(cands) > 1:
        print(f"[cookie] 检测到 {len(cands)} 个候选，默认使用 {path}"
              f"（其余: {[p for p, _ in cands[1:]]}；可用 --cookie-key 指定）", file=sys.stderr)
    else:
        print(f"[cookie] 使用 {path}", file=sys.stderr)
    return from_curl(val)

# ---------------- 自动抓取 key/host（+顺带尝试构建号） ----------------
def build_autofill_allowed(cfg: dict) -> bool:
    """只有空值或内置默认值才允许自动填充；任何非默认手填值都视为用户意图，不覆盖"""
    return not cfg.get("client_build") or cfg.get("client_build") == DEFAULT_BUILD

def autodiscover(file_id: str, cfg: dict, cfg_path: str):
    fresh = time.time() - cfg.get("_fetched_at", 0) < REFRESH_AFTER
    if cfg.get("api_key") and cfg.get("host") and fresh:
        return
    try:
        req = urllib.request.Request(f"https://drive.google.com/file/d/{file_id}/view",
                                     headers={"User-Agent": "Mozilla/5.0"})
        html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
        m = re.search(r'"([A-Za-z0-9_-]{20,})","(appsgenaiserver-pa[\w.-]*)"', html)
        if m:
            cfg["api_key"], cfg["host"] = m.group(1), m.group(2)
        b = re.search(r'goog_\d{6,}', html)      # 登录态 HTML 才可能有；匿名通常没有
        if b and build_autofill_allowed(cfg):
            cfg["client_build"] = b.group(0)
        cfg["_fetched_at"] = int(time.time())
        save_config(cfg_path, cfg)
    except Exception:
        if not (cfg.get("api_key") and cfg.get("host")):
            cfg["api_key"], cfg["host"] = DEFAULT_KEY, DEFAULT_HOST  # 兜底
    if not cfg.get("client_build"):
        cfg["client_build"] = DEFAULT_BUILD   # 匿名页面抓不到构建号，用内置值兜底

# ---------------- 协议 ----------------
def sapisidhash(cookie: str) -> str:
    m = re.search(r"(?:^|;\s*)SAPISID=([^;]+)", cookie)
    if not m: die(5, "cookie 串里找不到 SAPISID（确认复制的是完整请求头 cookie）")
    sid = m.group(1).strip()          # 尾部空格会导致 hash 不一致
    t = int(time.time())
    sig = f"{t}_{hashlib.sha1(f'{t} {sid} {ORIGIN}'.encode()).hexdigest()}"
    return f"SAPISIDHASH {sig}"

def build_body(file_id: str, prompt: str, cfg: dict) -> str:
    build = cfg.get("client_build") or DEFAULT_BUILD
    return json.dumps([[
        15, None,
        [19, None, None, None, build, [None, None, None, None, [0]], "1", None,
         [None, None, None,
          [[[None, None, None, None, None, None, None,
             [None, None, [None, None, None, [file_id, None, "application/pdf"]]]]]]],
         None, None, [3, 0], None, cfg["locale"],
         None, None, None, None, None, 1,
         None, None, None, None, None, None, None, None, uuid.uuid4().hex.upper(),
         [], 0, str(uuid.uuid4()).upper(), [str(uuid.uuid4()).upper()],
         None, cfg["timezone"],
         None, None, None, None, None, 0, None, None, None, None, [1, 1]],
        [None, None, prompt],
        None, [1, None, [[None, "1", 1182]]], 1],
        [1, None, 1]], ensure_ascii=False, separators=(",", ":"))

def extract_text(resp: str) -> str:
    """流式 JSON-protobuf → 文本。规则：整段先出、分段/累积分段为其子串 → 只保留互不包含的最大串。
    ponytail: 启发式提取（已知漏网：starter tiles/元数据串），若输出混入噪音，过滤规则在此加。"""
    try: tree = json.loads(resp)
    except Exception: return resp
    keep = []
    def walk(n):
        if isinstance(n, str):
            if len(n) < 6 or n.startswith(("/", "$", "http", "goog_")) or n.isdigit():
                return
            for k in keep:
                if n in k: return
            keep[:] = [k for k in keep if k not in n]
            keep.append(n)
        elif isinstance(n, list):
            for x in n: walk(x)
        elif isinstance(n, dict):
            for v in n.values(): walk(v)
    walk(tree)
    return "\n".join(keep)

def post(url: str, body: str, cookie: str, timeout: int = 180):
    req = urllib.request.Request(url, data=body.encode(), method="POST")
    req.add_header("content-type", "application/json+protobuf; charset=UTF-8")
    req.add_header("authorization", sapisidhash(cookie))
    req.add_header("cookie", cookie)
    req.add_header("user-agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                 "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
    # 铁律：不带 origin / x-origin（触发 400 XD3）
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, ssl.SSLError, TimeoutError, ConnectionError, OSError) as e:
        reason = getattr(e, "reason", e)
        die(6, f"网络异常: {reason}\n检查网络/代理后重试")

def status_exit(code: int, body: str) -> int:
    if code == 200: return 0
    if code == 401: die(2, "鉴权失败(401)：cookie 过期/不全，重新导出（AGENT_SPEC §4）")
    if code == 429: die(3, "配额不足(429)：" + body[:200])
    if code in (403, 404):
        die(4, f"{code}：api_key/host 可能过期，或文件不可访问/无权限。\n"
               f"已自动重新发现过仍失败 → 检查链接权限；或删掉 config 里 api_key/host 后重跑")
    if code == 400:
        if "XD3" in body: die(4, "XD3 拦截：本工具不应带 origin 头，检查代理是否注入（AGENT_SPEC §6.4）")
        die(4, "协议变更(400)：" + body[:300] + "\n按 AGENT_SPEC §6.1 更新 client_build")
    die(4, f"意外状态 {code}: {body[:200]}")

# ---------------- 入口 ----------------
def parse_file_id(target: str) -> str:
    """支持 /file/d/<id>/、open?id=<id>、uc?id=<id>、裸 ID；docs 链接给出警告（MIME 硬编码 pdf）"""
    m = (re.search(r"/d/([a-zA-Z0-9_-]{20,})", target)
         or re.search(r"[?&]id=([a-zA-Z0-9_-]{20,})", target))
    if "docs.google.com" in target:
        print("[warn] docs.google.com 链接：本工具 MIME 固定为 application/pdf，"
              "非 Drive PDF（Docs/Sheets 等）可能失败", file=sys.stderr)
    return m.group(1) if m else target

def parse_quota(body: str) -> str:
    """quotaSummary JSON-protobuf → 可读行。字段语义未逆向，按出现顺序取前两个数字启发式展示。
    json.loads 递归收集，兼容带引号/不带引号的数字。"""
    try:
        nums = []
        def walk(n):
            if len(nums) >= 2: return
            if isinstance(n, bool): return
            if isinstance(n, (int, float)):
                nums.append(n)
            elif isinstance(n, str) and re.fullmatch(r"\d+(\.\d+)?", n):
                nums.append(n)
            elif isinstance(n, list):
                for v in n: walk(v)
            elif isinstance(n, dict):
                for v in n.values(): walk(v)
        walk(json.loads(body))
        if len(nums) >= 2:
            return f"[quota] 剩余 {nums[0]} / 总量 {nums[1]}（字段语义为启发式解读）"
    except Exception:
        pass
    return "[quota] 无法解析，原始响应如下"

def main():
    ap = argparse.ArgumentParser(description="Drive Gemini 摘要（纯 HTTP）")
    ap.add_argument("target", nargs="?", help="Drive 链接或文件 ID（--quota 可省略）")
    ap.add_argument("--prompt", help="提示词（默认取配置文件）")
    ap.add_argument("--config", default=os.environ.get("DRIVE_CONFIG",
                       os.path.expanduser("~/.drive_summarize/config.json")))
    ap.add_argument("--out", help="输出到文件（默认 stdout）")
    ap.add_argument("--cookie-file", help="cookie JSON 文件路径（任意位置；最高优先级）")
    ap.add_argument("--cookie-key", help="JSON 里有多个候选时指定用哪个键（默认第一个并打印）")
    ap.add_argument("--client-build", help="手工指定客户端构建号（如 goog_1234567890），写入配置持久生效")
    ap.add_argument("--quota", action="store_true", help="只查配额（可不带文件链接）")
    ap.add_argument("--selftest", action="store_true", help="离线自检（不联网）")
    args = ap.parse_args()

    if args.selftest:
        selftest(); print("selftest OK"); return

    cfg = load_config(args.config)
    if args.client_build:          # 手工指定：持久写入；非默认值后自动发现不会再覆盖
        cfg["client_build"] = args.client_build
        save_config(args.config, cfg)
        print(f"[config] client_build 已设为 {args.client_build}", file=sys.stderr)
    cookie = load_cookie(cfg, args.config, args.cookie_file, args.cookie_key)

    if args.quota and not args.target:      # 配额查询不依赖具体文件
        if not (cfg.get("api_key") and cfg.get("host")):
            die(5, "无缓存 api_key/host：请先带文件链接跑一次（或设置 --config 指向已有配置）")
    else:
        if not args.target:
            die(5, "需要 Drive 链接或文件 ID（--help 看用法）")
        file_id = parse_file_id(args.target)
        autodiscover(file_id, cfg, args.config)

    if args.quota:
        def quota_url():
            return f"https://{cfg['host']}/v1/genai/quotaSummary?key={cfg['api_key']}"
        code, body = post(quota_url(), "[null,1,[19]]", cookie, timeout=30)
        if code in (403, 404):   # key/host 过期：与生成分支一致，重发现一次再试
            if args.target:
                print(f"[retry] {code} → 清空缓存 key/host 重新自动发现", file=sys.stderr)
                cfg.update({"api_key": None, "host": None, "_fetched_at": 0})
                autodiscover(file_id, cfg, args.config)
                code, body = post(quota_url(), "[null,1,[19]]", cookie, timeout=30)
            else:
                cfg.update({"api_key": None, "host": None, "_fetched_at": 0})
                save_config(args.config, cfg)
                die(4, f"{code}：缓存的 api_key/host 已失效并已清空——"
                       f"请带任意 Drive 文件链接跑一次以重新发现")
        status_exit(code, body)
        print(parse_quota(body))
        print(body); return

    def gen_url():
        return f"https://{cfg['host']}/v1/genai/streamGenerate?key={cfg['api_key']}"

    code, body = post(gen_url(), build_body(file_id, args.prompt or cfg["prompt"], cfg), cookie)
    if code in (403, 404):   # key/host 可能过期：强制重发现一次再试
        print(f"[retry] {code} → 清空缓存 key/host 重新自动发现", file=sys.stderr)
        cfg.update({"api_key": None, "host": None, "_fetched_at": 0})
        autodiscover(file_id, cfg, args.config)
        code, body = post(gen_url(), build_body(file_id, args.prompt or cfg["prompt"], cfg), cookie)
    status_exit(code, body)
    text = extract_text(body)
    if args.out:
        open(args.out, "w", encoding="utf-8").write(text + "\n")
        if os.name != "nt":
            os.chmod(args.out, 0o600)     # 摘要含文件内容，默认仅本人可读
        print(f"已写入 {args.out}", file=sys.stderr)
    else:
        print(text)

def selftest():
    # 1) SAPISIDHASH 结构
    cookie = "SID=x; SAPISID=TESTSID; HSID=y"
    h = sapisidhash(cookie)
    assert h.startswith("SAPISIDHASH ") and "_" in h and "TESTSID" not in h
    # 2) 请求体不变量（模板字段照抄位不被误改）
    cfg = {"client_build": "goog_TEST", "locale": "zh-CN", "timezone": "Asia/Shanghai"}
    raw = build_body("FILEID1234567890abcdefgh", "提示词内容", cfg)
    b = json.loads(raw)
    msg = b[0][2]
    assert msg[4] == "goog_TEST" and msg[13] == "zh-CN" and msg[34] == "Asia/Shanghai"
    assert '["FILEID1234567890abcdefgh",null,"application/pdf"]' in raw   # 文件ID+mime 位
    assert '"提示词内容"' in raw
    # 3) 文本提取：整段+分段+累积扩展，只留最大串且顺序稳定
    chunk = lambda s: [[[[[[s, ["x", s]]]]]]]      # 每条流式消息的嵌套形态
    resp = json.dumps([chunk("完整摘要第一段内容在此"),
                       chunk("完整摘要第一段内容在此还有第二段")])
    assert extract_text(resp) == "完整摘要第一段内容在此还有第二段"
    # 4) cookie JSON 多键探测：字符串形态 / 扁平映射形态 / cURL 粘贴 / 干扰键
    j = {"openai": "sk-xxx", "google": {"main": "SID=s; SAPISID=p; HSID=h"}, "n": 1}
    assert find_cookie_values(j) == [("$.google.main", "SID=s; SAPISID=p; HSID=h")]
    m = {"accts": {"a": {"SID": "s", "SAPISID": "p", "HSID": "h"}, "b": "noise"}}
    got = find_cookie_values(m)
    assert len(got) == 1 and "SID=s" in got[0][1] and "SAPISID=p" in got[0][1]
    assert from_curl("curl 'https://x' -H 'cookie: SID=a; SAPISID=b'") == "SID=a; SAPISID=b"
    assert from_curl("SID=a; SAPISID=b;") == "SID=a; SAPISID=b"
    # 5) cURL 其他形态 / 链接解析 / dict 节点文本提取 / 配额解析
    assert from_curl("curl 'x' --header 'Cookie: SID=a; SAPISID=b'") == "SID=a; SAPISID=b"
    assert from_curl("curl 'x' -b 'SID=a; SAPISID=b'") == "SID=a; SAPISID=b"
    fid = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
    assert parse_file_id(f"https://drive.google.com/file/d/{fid}/view") == fid
    assert parse_file_id(f"https://drive.google.com/open?id={fid}") == fid
    assert parse_file_id(f"https://drive.google.com/uc?export=download&id={fid}") == fid
    assert parse_file_id(fid) == fid
    assert extract_text(json.dumps({"k": [[[[[[["从dict里冒出来的文本内容", ["x"]]]]]]]]})) == "从dict里冒出来的文本内容"
    assert "剩余" in parse_quota('[[null,["999999",null,"888",3]]]')
    # 6) 配额解析兼容不带引号数字；构建号自动填充规则（空/默认→允许，手填非默认→保护）
    assert "999999" in parse_quota('[[null,[999999,null,888,3]]]')
    assert build_autofill_allowed({"client_build": None}) is True
    assert build_autofill_allowed({"client_build": DEFAULT_BUILD}) is True
    assert build_autofill_allowed({"client_build": "goog_9999999999"}) is False

if __name__ == "__main__":
    main()
