#!/usr/bin/env python3
"""
Drive「问问 Gemini」摘要 — 纯 HTTP 复放脚本（不开网页）
用法:
  1. 从已登录浏览器的 DevTools → Network → 任一 appsgenaiserver 请求 →
     请求头里的整串 cookie 复制下来（必须含 SAPISID、SID、HSID、SSID、__Secure-1PSID 等）
  2. python3 drive_gemini_summary.py "<drive文件ID或链接>" --cookie '<整串cookie>' [--prompt '总结这份文件']
协议: apps_intelligence.gen_ai.GenAiService/StreamGenerate @ appsgenaiserver-pa.clients6.google.com
      鉴权: SAPISIDHASH <t>_<sha1(t + " " + SAPISID + " " + origin)>  (需带正确 origin: https://drive.google.com；不要带 x-origin)
依赖: 仅标准库
"""
import argparse, hashlib, json, re, sys, time, urllib.request, uuid

KEY = "AIzaSyDVQw45DwoYh632gvsP5vPDqEKvb-Ywnb8"   # 公开浏览器 key（页面下发，非密钥）
URL = ("https://appsgenaiserver-pa.clients6.google.com/v1/genai/streamGenerate?key=" + KEY)
ORIGIN = "https://drive.google.com"
CLIENT_BUILD = "goog_618132029"                  # 2026-11-06 实测，随版本可能变化

def build_body(file_id: str, prompt: str) -> str:
    return json.dumps([[
        15, None,
        [19, None, None, None, CLIENT_BUILD, [None, None, None, None, [0]], "1", None,
         [None, None, None,
          [[[None, None, None, None, None, None, None,
             [None, None, [None, None, None, [file_id, None, "application/pdf"]]]]]]],
         None, None, [3, 0], None, "zh-CN",
         None, None, None, None, None, 1,
         None, None, None, None, None, None, None, None, uuid.uuid4().hex.upper(),
         [], 0, str(uuid.uuid4()).upper(), [str(uuid.uuid4()).upper()],
         None, "Asia/Shanghai",
         None, None, None, None, None, 0, None, None, None, None, [1, 1]],
        [None, None, prompt],
        None, [1, None, [[None, "1", 1182]]], 1
    ], [1, None, 1]], ensure_ascii=False, separators=(",", ":"))

def sapisidhash(sapisid: str) -> str:
    t = int(time.time())
    digest = hashlib.sha1(f"{t} {sapisid} {ORIGIN}".encode()).hexdigest()
    return f"SAPISIDHASH {t}_{digest}"

def extract_text(resp: str) -> str:
    """流式 JSON-protobuf → 文本；优先取最后一条消息的完整 Markdown。"""
    try:
        tree = json.loads(resp)
    except Exception:
        return resp
    messages = tree[0] if isinstance(tree, list) and tree and isinstance(tree[0], list) else []
    for msg in reversed(messages):
        try:
            text = msg[0][0][0][0][0][0][0]
            if isinstance(text, str) and len(text) >= 20:
                return text.strip()
        except Exception:
            pass
    seen, out = [], []
    def walk(node):
        if isinstance(node, str):
            if len(node) >= 6 and not node.startswith("$") and node not in seen:
                if not any(node in s for s in seen):
                    out.append(node)
                seen.append(node)
        elif isinstance(node, list):
            for x in node:
                walk(x)
    walk(tree)
    return "\n".join(out)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file", help="Drive 文件 ID 或完整链接")
    ap.add_argument("--cookie", required=True, help="浏览器复制的整串 cookie")
    ap.add_argument("--prompt", default="总结这份文件的要点。")
    args = ap.parse_args()

    m = re.search(r"/d/([a-zA-Z0-9_-]{20,})", args.file)
    file_id = m.group(1) if m else args.file

    sapisid = re.search(r"(?:^|;\s*)SAPISID=([^;]+)", args.cookie)
    if not sapisid:
        sys.exit("cookie 里找不到 SAPISID，请复制完整请求头 cookie")
    cookie = args.cookie.strip().rstrip(";")

    req = urllib.request.Request(URL, data=build_body(file_id, args.prompt).encode(), method="POST")
    req.add_header("content-type", "application/json+protobuf; charset=UTF-8")
    req.add_header("authorization", sapisidhash(sapisid.group(1)))
    req.add_header("cookie", cookie)
    req.add_header("origin", "https://drive.google.com")
    req.add_header("referer", "https://drive.google.com/")
    req.add_header("user-agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                 "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
    # 2026-11-06 实测: 正确 origin 必需；错误 origin / x-origin 会触发 XD3 拦截

    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read().decode("utf-8", "replace")
    print(extract_text(raw))

if __name__ == "__main__":
    main()
