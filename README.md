# drive-gemini-summary

Fetch the **Gemini summary** ("Ask Gemini" / 摘要) of any Google Drive PDF you can access — with a single pure-HTTP call, no browser, no page rendering.

This is a reverse-engineered client for the internal endpoint Drive's web viewer uses
(`apps_intelligence.gen_ai.GenAiService/StreamGenerate` @ `appsgenaiserver-pa.clients6.google.com`).
Requests run under **your own** Google account and consume your own Gemini quota
(a Google AI Pro / Workspace Gemini subscription is required — same as the web UI).

## How it works

```
Drive link ──► CLI ──► POST /v1/genai/streamGenerate
                       Authorization: SAPISIDHASH(你的 SAPISID cookie)
                       body: [fileId, "application/pdf", prompt, ...]  (JSON-protobuf)
             ◄── streamed markdown summary
```

## Quick start

```bash
python3 drive_summarize.py "https://drive.google.com/file/d/<FILE_ID>/view"
```

First run generates `~/.drive_summarize/config.json` and tells you where to put cookies.

**Cookie setup (the only manual step):** in a browser logged into your Gemini-enabled
account, open any Drive PDF → F12 → Network → filter `appsgenaiserver` → trigger a
summary → copy the request's `cookie` header (or use "Copy as cURL" wholesale).
Save it as JSON anywhere — extra keys are fine, the CLI locates the `SAPISID` value automatically:

```json
{ "anything": "SID=...; SAPISID=...; HSID=...; SSID=..." }
```

```bash
python3 drive_summarize.py <link> --cookie-file /path/to/creds.json   # any location
python3 drive_summarize.py <link> --cookie-file creds.json --cookie-key work   # multi-account
python3 drive_summarize.py <link> --prompt "List the key points" --out out.md
python3 drive_summarize.py <link> --quota       # check remaining Gemini quota
python3 drive_summarize.py --selftest           # offline checks, no network
```

Everything else is automatic: API key & host are fetched from the Drive page and cached,
`SAPISIDHASH` and UUIDs are generated per request. Exit codes: `0` ok, `2` auth (re-export
cookies), `3` quota, `4` protocol change (update `client_build`), `5` config missing.

## Repo layout

| File | Purpose |
|---|---|
| `drive_summarize.py` | The CLI (stdlib only) |
| `drive_gemini_summary.py` | Minimal single-file reference implementation |
| `AGENT_SPEC.md` | Full protocol spec + error table + maintenance playbook (中文) |
| `RECON.md` | How the protocol was reverse-engineered, incl. the auth pitfall matrix |

## Notes & limits

- Never send `origin`/`x-origin` headers with the request — it triggers a `400 XD3` rejection.
- The client build id (`goog_1178929964` at the time of reversing) drifts with Drive releases;
  see AGENT_SPEC §6.1 for the 1-minute fix when it breaks.
- Cookies are full account credentials: keep the JSON file private (`chmod 600`).
- Personal-use tool: your account, your quota, your files. No public API exists for this
  feature; treat breakage as expected whenever Google ships changes.

## Disclaimer

For educational purposes and personal automation of your own account. Not affiliated
with Google. Respect Google's Terms of Service.
