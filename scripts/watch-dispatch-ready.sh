#!/usr/bin/env bash
# 監看派工路徑：一旦 leader 進得去三個 worker 頻道，就發一則測試派工並觀察反應。
# 供 Monitor 使用——每次輪詢輸出一行狀態，成功後多輸出結果並結束。
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

SECRETS=config/secrets.yaml
INPUT=config/catalog-input.json
ADMIN="$HOME/.kube/oab-admin.kubeconfig"

python3 - "$SECRETS" "$INPUT" "$ADMIN" <<'PY'
import json, sys, time, subprocess, urllib.request, urllib.error
import yaml

secrets = yaml.safe_load(open(sys.argv[1]))["secrets"]
channels = json.load(open(sys.argv[2]))["channels"]
admin_kubeconfig = sys.argv[3]
leader_t = secrets["discord-leader/token"]
DEV_CH = channels["developer"]
DEV_BOT = "1544235162420903966"
WORKERS = ("researcher", "developer", "reviewer")

def api(path, token, method="GET", body=None):
    req = urllib.request.Request(
        f"https://discord.com/api/v10{path}",
        data=json.dumps(body).encode() if body else None, method=method,
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                 "User-Agent": "oab-watch/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
        return json.loads(raw) if raw else None

def leader_can_reach(ch):
    try:
        api(f"/channels/{ch}/messages?limit=1", leader_t)
        return True
    except urllib.error.HTTPError:
        return False

deadline = time.time() + 3300
while time.time() < deadline:
    blocked = [w for w in WORKERS if not leader_can_reach(channels[w])]
    if not blocked:
        print("派工路徑已開通——leader 進得去三個 worker 頻道", flush=True)
        break
    print(f"仍待設定：leader 進不去 {', '.join('#'+w for w in blocked)}", flush=True)
    time.sleep(20)
else:
    print("逾時：3300 秒內未偵測到派工路徑開通", flush=True)
    sys.exit(0)

# 發一則測試派工
msg = api(f"/channels/{DEV_CH}/messages", leader_t, "POST", {
    "content": f"<@{DEV_BOT}> 這是 oab-control 的派工連通性測試。"
               f"請只回覆一句「收到，等待正式任務」，不要執行任何工作。"
})
print(f"leader 已發送測試派工 message_id={msg['id']}", flush=True)

time.sleep(25)

# 讀 developer 的回覆
replies = api(f"/channels/{DEV_CH}/messages?limit=5&after={msg['id']}", leader_t)
dev_replies = [m for m in (replies or []) if m["author"]["id"] == DEV_BOT]
if dev_replies:
    text = dev_replies[-1]["content"][:280].replace("\n", " ")
    print(f"developer agent 已回覆：{text}", flush=True)
else:
    print("developer agent 尚未回覆（可能仍在思考，或需 rollout restart 讓 harness 重讀）", flush=True)

# harness 日誌佐證
try:
    log = subprocess.run(
        ["kubectl", "-n", "oab-agents", "logs", "deploy/developer", "--tail=12"],
        env={"KUBECONFIG": admin_kubeconfig, "PATH": "/usr/local/bin:/usr/bin:/bin"},
        capture_output=True, text=True, timeout=30).stdout
    import re
    for line in log.splitlines()[-6:]:
        clean = re.sub(r"\x1b\[[0-9;]*m", "", line)
        if any(k in clean for k in ("discord", "session", "message", "acp", "error", "ERROR")):
            print(f"  log: {clean[:200]}", flush=True)
except Exception:
    pass

print("派工驗證完成", flush=True)
PY
