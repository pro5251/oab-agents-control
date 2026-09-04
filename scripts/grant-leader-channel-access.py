#!/usr/bin/env python3
"""Grant the leader role access to each worker's private channel.

Dispatch is the leader posting a mention in a worker's private channel.  Those
channels are created granting access to the worker alone, so the leader gets
403 and the message never arrives -- with nothing logged anywhere.

The fix is one permission overwrite per worker channel: the `leader` role
(which the leader bot holds) allowed to view, send, and read history.

This needs a token with MANAGE_ROLES on those channels.  The agent bots do not
have it -- deliberately; letting an agent reshape the server would be the
largest grant in the design.  So run this with a token that does:

    OAB_ADMIN_BOT_TOKEN=<token> python3 scripts/grant-leader-channel-access.py

or do it by hand in Discord:
    each worker channel -> Edit Channel -> Permissions -> add the `leader` role
    -> allow View Channel, Send Messages, Read Message History
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

import yaml

API = "https://discord.com/api/v10"
GUILD = None  # discovered from the leader bot
INPUT = "config/catalog-input.json"
SECRETS = "config/secrets.yaml"

VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
READ_HISTORY = 1 << 16
ALLOW = VIEW_CHANNEL | SEND_MESSAGES | READ_HISTORY

WORKERS = ("researcher", "developer", "reviewer")


def api(path: str, token: str, *, method: str = "GET", body: dict | None = None):
    request = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "oab-control-grant/1.0",
        },
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            if error.code == 429 and attempt < 3:
                time.sleep(float(error.headers.get("Retry-After") or 3) + 0.5)
                continue
            raise


def main() -> int:
    admin = os.environ.get("OAB_ADMIN_BOT_TOKEN")
    secrets = yaml.safe_load(Path(SECRETS).read_text(encoding="utf-8"))["secrets"]
    leader_token = secrets["discord-leader/token"]

    guilds = api("/users/@me/guilds", leader_token)
    guild = guilds[0]["id"]
    roles = {r["name"]: r["id"] for r in api(f"/guilds/{guild}", leader_token)["roles"]}
    if "leader" not in roles:
        raise SystemExit("找不到 'leader' 身分組——請確認 leader bot 有一個同名角色")
    leader_role = roles["leader"]

    channels = json.loads(Path(INPUT).read_text(encoding="utf-8"))["channels"]

    print(f"leader 身分組：{leader_role}\n")
    print("目標：為每個 worker 頻道加上 leader 角色覆寫（+檢視 +傳送 +歷史）\n")

    if not admin:
        print("未提供 OAB_ADMIN_BOT_TOKEN——只顯示需要的變更，不寫入：\n")
        for role in WORKERS:
            print(f"  #{role}（{channels[role]}）: 加入 role={leader_role} allow={ALLOW}")
        print("\n在 Discord 手動操作，或設定 OAB_ADMIN_BOT_TOKEN 後重跑。")
        return 0

    failed = 0
    for role in WORKERS:
        channel = channels[role]
        try:
            api(
                f"/channels/{channel}/permissions/{leader_role}",
                admin,
                method="PUT",
                body={"type": 0, "allow": str(ALLOW), "deny": "0"},
            )
            print(f"  ✅ #{role} 已授權")
        except urllib.error.HTTPError as error:
            message = ""
            try:
                message = json.load(error).get("message", "")
            except Exception:
                pass
            print(f"  ❌ #{role} HTTP {error.code} {message}")
            failed += 1

    print()
    print("驗證：")
    for role in WORKERS:
        try:
            api(f"/channels/{channels[role]}/messages?limit=1", leader_token)
            print(f"  ✅ leader 現在讀得到 #{role}")
        except urllib.error.HTTPError as error:
            print(f"  ❌ leader 仍無法讀取 #{role}（HTTP {error.code}）")
            failed += 1

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
