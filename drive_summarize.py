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

Cookie 文件（--cookie-file 可传任意路径，优先级：参数 > 环境变量 > 配置）：
  JSON 格式，多键无所谓 —— 递归自动定位含 SAPISID 的那个值，支持三种形态：
    {"google": "SID=...; SAPISID=...; HSID=..."}          ← 值=整串 cookie
    {"google": "curl '...' -H 'cookie: SID=...;'"}        ← 值=Copy as cURL 命令
    {"google": {"SID": "...", "SAPISID": "...", ...}}     ← 值=扁平 cookie 映射
  多个账号候选时默认用第一个，--cookie-key <键名> 指定；运行时会打印用了哪个键。
环境变量: DRIVE_COOKIE（整串值直接覆盖）/ DRIVE_COOKIE_FILE / DRIVE_CONFIG。
退出码: 0 成功 | 2 鉴权失败 | 3 配额 | 4 协议变更(400) | 5 配置缺失
"""
import argparse, hashlib, json, os, re, sys, time, urllib.request, uuid

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
           "_fetched_at": 0, "_build_src": "builtin"}
    if os.path.exists(path):
        try:
            cfg.update(json.load(open(path, encoding="utf-8")))
        except Exception as e:
            die(5, f"配置文件解析失败 {path}: {e}")
    else:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        json.dump(cfg, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"已生成配置模板: {path}\n请把 cookie 存到 "
              f"{os.path.join(os.path.dirname(os.path.abspath(path)), cfg['cookie_file'])}"
              f"（JSON，多键随意，自动定位含 SAPISID 的值）", file=sys.stderr)
    return cfg

def save_config(path: str, cfg: dict):
    tmp = {k: v for k, v in cfg.items()}
    json.dump(tmp, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

# ---------------- Cookie ----------------
def from_curl(text: str) -> str:
    """Copy as cURL 粘贴 → 抽 cookie 头；非 cURL 原样返回"""
    if not text.lstrip().startswith("curl "):
        return text.strip().rstrip(";")
    m = (re.search(r"-H\s+[\"']cookie:\s*([^\"']+)[\"']", text, re.I)
         or re.search(r"--cookie\s+[\"']([^\"']+)[\"']", text, re.I))
    if not m: die(5, "从 cURL 命令里没找到 cookie 头")
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
    raw = os.environ.get("DRIVE_COOKIE")
    if raw:
        return from_curl(raw)
    cf = (cookie_file or os.environ.get("DRIVE_COOKIE_FILE")
          or os.path.join(os.path.dirname(os.path.abspath(cfg_path)), cfg["cookie_file"]))
    if not os.path.exists(cf):
        die(5, f"缺少 cookie 文件 {cf}\nJSON 格式示例（多键随意，会自动定位含 SAPISID 的值）:\n"
               f'  {{"openai": "sk-...", "google": "SID=...; SAPISID=...; HSID=..."}}\n'
               f"获取: 已登录浏览器 F12 → Network → 过滤 appsgenaiserver → 点一条请求 → "
               f"Request Headers → cookie 整串（或 Copy as cURL 命令）填进任意键值")
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
        if b and cfg.get("_build_src") != "manual":
            cfg["client_build"], cfg["_build_src"] = b.group(0), "auto"
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
    t = int(time.time())
    sig = f"{t}_{hashlib.sha1(f'{t} {m.group(1)} {ORIGIN}'.encode()).hexdigest()}"
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

def status_exit(code: int, body: str) -> int:
    if code == 200: return 0
    if code == 401: die(2, "鉴权失败(401)：cookie 过期/不全，重新导出（AGENT_SPEC §4）")
    if code == 429: die(3, "配额不足(429)：" + body[:200])
    if code == 400:
        if "XD3" in body: die(4, "XD3 拦截：本工具不应带 origin 头，检查代理是否注入（AGENT_SPEC §6.4）")
        die(4, "协议变更(400)：" + body[:300] + "\n按 AGENT_SPEC §6.1 更新 client_build")
    die(4, f"意外状态 {code}: {body[:200]}")

# ---------------- 入口 ----------------
def main():
    ap = argparse.ArgumentParser(description="Drive Gemini 摘要（纯 HTTP）")
    ap.add_argument("target", nargs="?", help="Drive 链接或文件 ID")
    ap.add_argument("--prompt", help="提示词（默认取配置文件）")
    ap.add_argument("--config", default=os.environ.get("DRIVE_CONFIG",
                       os.path.expanduser("~/.drive_summarize/config.json")))
    ap.add_argument("--out", help="输出到文件（默认 stdout）")
    ap.add_argument("--cookie-file", help="cookie JSON 文件路径（任意位置；优先于配置与环境变量）")
    ap.add_argument("--cookie-key", help="JSON 里有多个候选时指定用哪个键（默认第一个并打印）")
    ap.add_argument("--quota", action="store_true", help="只查配额")
    ap.add_argument("--selftest", action="store_true", help="离线自检（不联网）")
    args = ap.parse_args()

    if args.selftest:
        selftest(); print("selftest OK"); return

    if not args.target:
        die(5, "需要 Drive 链接或文件 ID（--help 看用法）")
    m = re.search(r"/d/([a-zA-Z0-9_-]{20,})", args.target)
    file_id = m.group(1) if m else args.target

    cfg = load_config(args.config)
    cookie = load_cookie(cfg, args.config, args.cookie_file, args.cookie_key)
    autodiscover(file_id, cfg, args.config)

    if args.quota:
        code, body = post(f"https://{cfg['host']}/v1/genai/quotaSummary?key={cfg['api_key']}",
                          "[null,1,[19]]", cookie, timeout=30)
        status_exit(code, body)
        print(body); return

    code, body = post(f"https://{cfg['host']}/v1/genai/streamGenerate?key={cfg['api_key']}",
                      build_body(file_id, args.prompt or cfg["prompt"], cfg), cookie)
    status_exit(code, body)
    text = extract_text(body)
    if args.out:
        open(args.out, "w", encoding="utf-8").write(text + "\n")
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

if __name__ == "__main__":
    main()
