#!/usr/bin/env python3
"""Bind catalog agents to real Discord channels, and check they are usable.

Channels have to be created by a human: none of the agent bots hold
MANAGE_CHANNELS, and giving an agent the ability to reshape the server it
operates in would be a far larger grant than anything else in this design.

So this script does the part that does not need that permission -- find the
channels by name, confirm each bot can actually use the one assigned to it,
and write the IDs into config/catalog-input.json.

Usage:
    python3 scripts/bind-discord-channels.py                  # discover by name
    python3 scripts/bind-discord-channels.py --check          # verify only
    python3 scripts/bind-discord-channels.py \\
        --map leader=123,researcher=456,developer=789,reviewer=012

Expected channel names (override with --prefix):
    oab-leader, oab-researcher, oab-developer, oab-reviewer
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

import yaml

ROLES = ("leader", "researcher", "developer", "reviewer")
API = "https://discord.com/api/v10"
VIEW_CHANNEL = 1 << 10
TEXT_CHANNEL = 0


def api(path: str, token: str, *, attempts: int = 4) -> object:
    """Call the Discord API, honouring the rate limit it asks for.

    Binding four agents means several calls per channel, which is enough to hit
    the per-route limit.  A 429 carries the wait in Retry-After, so respect it
    rather than failing the whole run.
    """

    request = urllib.request.Request(
        f"{API}{path}",
        headers={"Authorization": f"Bot {token}", "User-Agent": "oab-control-bind/1.0"},
    )
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code != 429 or attempt == attempts - 1:
                raise
            wait = error.headers.get("Retry-After") or "2"
            try:
                delay = min(float(wait), 30.0)
            except ValueError:
                delay = 2.0
            print(f"    （Discord 限流，等待 {delay:.1f}s）", file=sys.stderr)
            time.sleep(delay + 0.5)
    raise SystemExit("Discord 持續限流，請稍後再試")


def tokens(secrets_path: Path) -> dict[str, str]:
    document = yaml.safe_load(secrets_path.read_text(encoding="utf-8"))
    values = (document or {}).get("secrets", {})
    found = {role: values.get(f"discord-{role}/token") for role in ROLES}
    missing = [role for role, token in found.items() if not token]
    if missing:
        raise SystemExit(f"config/secrets.yaml 缺少：{', '.join(missing)}")
    return found


def guild_id(token: str) -> str:
    guilds = api("/users/@me/guilds", token)
    if not guilds:
        raise SystemExit("bot 不在任何伺服器中")
    if len(guilds) > 1:
        names = ", ".join(f"{g['name']}={g['id']}" for g in guilds)
        raise SystemExit(f"bot 在多個伺服器中，請用 --guild 指定：{names}")
    return guilds[0]["id"]


def resolve(args, token_map: dict[str, str], guild: str) -> dict[str, str]:
    if args.map:
        chosen = {}
        for pair in args.map.split(","):
            role, _, value = pair.partition("=")
            if role.strip() not in ROLES:
                raise SystemExit(f"未知角色：{role}")
            chosen[role.strip()] = value.strip()
        missing = [role for role in ROLES if role not in chosen]
        if missing:
            raise SystemExit(f"--map 缺少：{', '.join(missing)}")
        return chosen

    channels = api(f"/guilds/{guild}/channels", token_map["leader"])
    by_name = {c["name"]: c for c in channels if c["type"] == TEXT_CHANNEL}
    chosen, missing = {}, []
    for role in ROLES:
        name = f"{args.prefix}{role}"
        if name in by_name:
            chosen[role] = by_name[name]["id"]
        else:
            missing.append(name)
    if missing:
        available = ", ".join(sorted(by_name)) or "（無文字頻道）"
        raise SystemExit(
            "找不到這些頻道：" + ", ".join(missing)
            + f"\n伺服器現有文字頻道：{available}"
            + "\n請先建立，或用 --map 指定既有頻道的 ID。"
        )
    return chosen


def check_dispatch(chosen: dict[str, str], token_map: dict[str, str]) -> bool:
    """Confirm the leader can reach the channels it dispatches into.

    Checking only that each bot sees its own channel misses the failure that
    actually matters: private worker channels typically grant access to the
    worker alone, so the leader gets 403 and dispatch is impossible.  Nothing
    in the control plane notices -- the message simply never arrives.
    """

    leader_token = token_map["leader"]
    ok = True
    for role in ROLES:
        if role == "leader":
            continue
        try:
            api(f"/channels/{chosen[role]}/messages?limit=1", leader_token)
            print(f"  ✅ leader → #{role} 可存取")
        except urllib.error.HTTPError as error:
            print(f"  ❌ leader → #{role} HTTP {error.code}——leader 無法在此頻道派工")
            ok = False
    return ok


def check(chosen: dict[str, str], token_map: dict[str, str], guild: str) -> bool:
    """Confirm each bot can actually see and post in its own channel."""

    ok = True
    for role in ROLES:
        channel_id = chosen[role]
        token = token_map[role]
        try:
            channel = api(f"/channels/{channel_id}", token)
        except urllib.error.HTTPError as error:
            print(f"  ❌ {role:11} 無法讀取頻道 {channel_id}（HTTP {error.code}）——bot 看不到它")
            ok = False
            continue
        if channel.get("guild_id") != guild:
            print(f"  ❌ {role:11} 頻道不屬於此伺服器")
            ok = False
            continue
        if channel.get("type") != TEXT_CHANNEL:
            print(f"  ❌ {role:11} 不是文字頻道（type={channel.get('type')}）")
            ok = False
            continue

        everyone = next(
            (o for o in channel.get("permission_overwrites", []) if o["id"] == guild), None
        )
        private = bool(everyone and int(everyone.get("deny", 0)) & VIEW_CHANNEL)

        # Read one message rather than reasoning about overwrites: permissions
        # can come from a role, so an overwrite-only check reports a problem
        # that is not there.  If this call succeeds, the bot really can see the
        # channel it is about to be assigned.
        try:
            api(f"/channels/{channel_id}/messages?limit=1", token)
            readable = True
        except urllib.error.HTTPError:
            readable = False

        note = "私有" if private else "⚠️ 公開"
        if role != "leader" and not private:
            note += "（worker 頻道建議設為私有）"
        if readable:
            print(f"  ✅ {role:11} #{channel['name']:22} {channel_id}  {note}")
        else:
            print(f"  ❌ {role:11} #{channel['name']:22} {channel_id}  {note}——bot 讀不到訊息")
            ok = False
    return ok


def write(chosen: dict[str, str], input_path: Path) -> None:
    document = json.loads(input_path.read_text(encoding="utf-8"))
    before = dict(document.get("channels", {}))
    document["channels"] = {role: chosen[role] for role in ROLES}
    input_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for role in ROLES:
        old, new = before.get(role, "—"), chosen[role]
        mark = "  " if old == new else "→ "
        print(f"  {mark}{role:11} {old} {'' if old == new else '→ ' + new}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--secrets", default="config/secrets.yaml")
    parser.add_argument("--input", default="config/catalog-input.json")
    parser.add_argument("--guild")
    parser.add_argument("--prefix", default="oab-", help="頻道名稱前綴（預設 oab-）")
    parser.add_argument("--map", help="明確指定：leader=ID,researcher=ID,...")
    parser.add_argument("--check", action="store_true", help="只驗證，不寫入")
    args = parser.parse_args()

    token_map = tokens(Path(args.secrets))
    guild = args.guild or guild_id(token_map["leader"])
    print(f"伺服器：{guild}\n")

    chosen = resolve(args, token_map, guild)
    print("驗證每個 bot 能否使用分配到的頻道：")
    healthy = check(chosen, token_map, guild)
    print()
    print("驗證派工路徑（leader 必須能進入每個 worker 的頻道）：")
    dispatchable = check_dispatch(chosen, token_map)
    print()
    if not dispatchable:
        print("修正方式：在 Discord 開啟該頻道 → 編輯頻道 → 權限 → 新增成員/身分組")
        print("           → 加入 leader bot，允許「檢視頻道」「傳送訊息」「讀取訊息紀錄」")
        print()
    healthy = healthy and dispatchable

    if args.check:
        return 0 if healthy else 1
    if not healthy:
        print("有頻道無法使用，未寫入 catalog-input.json。")
        return 1

    print("寫入 config/catalog-input.json：")
    write(chosen, Path(args.input))
    print("\n接著執行：")
    print("  python3 scripts/generate-catalog.py > config/catalog.yaml")
    print("  然後重新部署（見 docs/本機部署實作.md 的「更新」）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
