#!/usr/bin/env bash
# ============================================================================
# 端到端驗證：leader 分派任務的完整生命週期與每一道閘門
#
# 這不是示範腳本，是驗證腳本——每一步都斷言預期結果，包括「應該失敗」的那些。
# 閘門若被放寬，這裡會失敗。
#
# 使用獨立的暫存 tasks-dir，不會動到 .oab-control/tasks 的真實任務。
#
# 用法：bash scripts/verify-task-flow.sh [catalog]
# ============================================================================
set -uo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CATALOG="${1:-$PROJECT/config/catalog.yaml}"
cd "$PROJECT"
export PYTHONPATH=.

TASKS="$(mktemp -d)/tasks"
TMP="$(mktemp -d)"
trap 'rm -rf "$TASKS" "$TMP"' EXIT

pass=0; fail=0
say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m✅\033[0m %s\n' "$*"; pass=$((pass+1)); }
bad()  { printf '    \033[31m❌\033[0m %s\n' "$*"; fail=$((fail+1)); }

cli() { python3 -m oab_control.cli "$@" --json 2>&1; }

# 期望成功：跑得過且回傳的 state 相符
expect_state() { # 說明 期望state 指令...
  local desc="$1" want="$2"; shift 2
  local out; out="$("$@" 2>&1)"
  local got; got="$(printf '%s' "$out" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("state",""))
except Exception: print("")' 2>/dev/null)"
  [ "$got" = "$want" ] && ok "$desc（state=$got）" || bad "$desc 期望 state=$want 得到 '$got' — $(printf '%s' "$out" | head -c 160)"
}

# 期望被閘門擋下：必須失敗，且錯誤訊息含關鍵字
expect_blocked() { # 說明 關鍵字 指令...
  local desc="$1" needle="$2"; shift 2
  local out; out="$("$@" 2>&1)"
  if printf '%s' "$out" | grep -q "$needle"; then
    ok "$desc（正確拒絕）"
  else
    bad "$desc 未被擋下或訊息不符 — $(printf '%s' "$out" | head -c 200)"
  fi
}

# ---------------------------------------------------------------------------
say "0/8  從 catalog 取出 developer 的真實路由資訊"
python3 - "$CATALOG" > "$TMP/routing.env" <<'PY'
import sys, json
from oab_control.catalog import load_catalog
c, d = load_catalog(sys.argv[1], check_paths=False)
assert not d, d
a = c["agents"]["developer"]
g = next(x for x in a["repository_grants"] if x["access"] == "write")
print(f'REPO={g["repository"]}')
print(f'SUB={g["checkout_subpath"]}')
print(f'BASE={g["base_branch"]}')
print(f'WT={a["worktree"]["path"]}')
print(f'MOUNT={a["worktree"]["container_mount_path"]}/{g["checkout_subpath"]}')
print(f'IDENT={a["delivery"]["gitlab_identity_ref"]}')
print(f'REPLY={a["discord"]["work_channel_id"]}')
PY
[ -s "$TMP/routing.env" ] || { bad "無法讀取 catalog"; exit 1; }
# shellcheck disable=SC1090
. "$TMP/routing.env"
ok "developer 的 write grant：$SUB（base=$BASE）"

cat > "$TMP/task.json" <<EOF
{
  "task_id": "verify-flow",
  "kind": "code",
  "goal": "端到端驗證用的任務。",
  "scope": "僅驗證流程，不產生實際變更。",
  "canonical_sources": ["docs/"],
  "agent_id": "developer",
  "repository": "$REPO",
  "checkout_subpath": "$SUB",
  "worktree_path": "$WT",
  "container_mount_path": "$MOUNT",
  "branch": "task/verify-flow",
  "base_branch": "$BASE",
  "delivery_owner": "developer",
  "gitlab_identity_ref": "$IDENT",
  "tests": ["python3 -m unittest"],
  "completion_marker": "驗證完成。",
  "checkpoint": "回報結果。",
  "deadline": "2099-12-31T12:00:00Z",
  "reply_to": "$REPLY",
  "commit_authorized": true,
  "push_authorized": true,
  "mr_authorized": true
}
EOF

# ---------------------------------------------------------------------------
say "1/8  leader 建立任務（只有 leader 能寫入）"
expect_state "建立任務" planned \
  cli task-create "$TMP/task.json" --catalog "$CATALOG" --no-path-check --tasks-dir "$TASKS"
expect_blocked "worker 不能自行建立任務" "only the configured leader" \
  cli task-create "$TMP/task.json" --catalog "$CATALOG" --no-path-check --tasks-dir "$TASKS" --actor developer

# ---------------------------------------------------------------------------
say "2/8  leader 解析派工目標頻道"
# shellcheck disable=SC2181
out="$(python3 - "$CATALOG" "$TASKS" <<'PY'
import sys
from oab_control.catalog import load_catalog
from oab_control.tasks import TaskStore
from oab_control.discord_policy import dispatch_channel, DiscordPolicyError
c, _ = load_catalog(sys.argv[1], check_paths=False)
t = TaskStore(sys.argv[2]).get("verify-flow")
ch = dispatch_channel(c, agent_id="developer", task_envelope=t.envelope())
print("OK" if ch == c["agents"]["developer"]["discord"]["work_channel_id"] else "MISMATCH")
try:
    dispatch_channel(c, agent_id="leader", task_envelope=t.envelope()); print("LEADER_NOT_BLOCKED")
except DiscordPolicyError: print("LEADER_BLOCKED")
PY
)"
printf '%s' "$out" | grep -q "^OK$" && ok "派工目標解析為 developer 的私有頻道" || bad "派工目標不符"
printf '%s' "$out" | grep -q "LEADER_BLOCKED" && ok "不可把任務派給 leader 自己" || bad "竟可派給 leader"

# ---------------------------------------------------------------------------
say "3/8  推進到 active"
expect_state "planned → assigned" assigned cli task-transition verify-flow assigned --tasks-dir "$TASKS"
expect_state "assigned → active"  active   cli task-transition verify-flow active   --tasks-dir "$TASKS"

# ---------------------------------------------------------------------------
say "4/8  worker 回報與獨立審查"
printf '改動摘要與測試結果。\n' > "$TMP/report.md"
printf '獨立審查意見。\n' > "$TMP/review.md"
cli task-report verify-flow "$TMP/report.md" --tasks-dir "$TASKS" >/dev/null && ok "leader 轉錄 worker 回報" || bad "回報失敗"
expect_blocked "review 狀態前不可寫入審查" "review can only be recorded in review state" \
  cli task-review verify-flow "$TMP/review.md" --tasks-dir "$TASKS"
expect_state "active → review" review cli task-transition verify-flow review --tasks-dir "$TASKS"
cli task-review verify-flow "$TMP/review.md" --tasks-dir "$TASKS" >/dev/null && ok "記錄獨立審查" || bad "審查失敗"

# ---------------------------------------------------------------------------
say "5/8  驗收閘門（缺證據必須被擋）"
echo '{}' > "$TMP/empty.json"
expect_blocked "空證據不可驗收" "code acceptance missing" \
  cli task-transition verify-flow accepted --tasks-dir "$TASKS" --evidence-file "$TMP/empty.json"

cat > "$TMP/no-human.json" <<EOF
{"developer_tests":"passed","independent_review":"ok","ci_success":"yes","ci_status":"success",
 "leader_summary":"done","commit_sha":"abc123","push_ref":"refs/heads/task/verify-flow",
 "merge_request":"!1","delivery_repository":"$REPO","delivery_branch":"task/verify-flow",
 "delivery_owner":"developer","human_merge_authorized":false,
 "authorization_actor":"human","authorization_at":"2026-01-01T00:00:00Z","authorization_scope":"merge"}
EOF
expect_blocked "缺人類 merge 授權不可驗收" "human_merge_authorized" \
  cli task-transition verify-flow accepted --tasks-dir "$TASKS" --evidence-file "$TMP/no-human.json"

cat > "$TMP/wrong-ci.json" <<EOF
{"developer_tests":"passed","independent_review":"ok","ci_success":"yes","ci_status":"failed",
 "leader_summary":"done","commit_sha":"abc123","push_ref":"refs/heads/task/verify-flow",
 "merge_request":"!1","delivery_repository":"$REPO","delivery_branch":"task/verify-flow",
 "delivery_owner":"developer","human_merge_authorized":true,
 "authorization_actor":"human","authorization_at":"2026-01-01T00:00:00Z","authorization_scope":"merge"}
EOF
expect_blocked "CI 非 success 不可驗收" "successful GitLab CI" \
  cli task-transition verify-flow accepted --tasks-dir "$TASKS" --evidence-file "$TMP/wrong-ci.json"

cat > "$TMP/wrong-branch.json" <<EOF
{"developer_tests":"passed","independent_review":"ok","ci_success":"yes","ci_status":"success",
 "leader_summary":"done","commit_sha":"abc123","push_ref":"refs/heads/task/verify-flow",
 "merge_request":"!1","delivery_repository":"$REPO","delivery_branch":"task/OTHER",
 "delivery_owner":"developer","human_merge_authorized":true,
 "authorization_actor":"human","authorization_at":"2026-01-01T00:00:00Z","authorization_scope":"merge"}
EOF
expect_blocked "交付分支與任務不符不可驗收" "does not match the task owner" \
  cli task-transition verify-flow accepted --tasks-dir "$TASKS" --evidence-file "$TMP/wrong-branch.json"

cat > "$TMP/good.json" <<EOF
{"developer_tests":"passed","independent_review":"ok","ci_success":"yes","ci_status":"success",
 "leader_summary":"done","commit_sha":"abc123","push_ref":"refs/heads/task/verify-flow",
 "merge_request":"!1","delivery_repository":"$REPO","delivery_branch":"task/verify-flow",
 "delivery_owner":"developer","human_merge_authorized":true,
 "authorization_actor":"human","authorization_at":"2026-01-01T00:00:00Z","authorization_scope":"merge"}
EOF
expect_state "證據齊全才可驗收" accepted \
  cli task-transition verify-flow accepted --tasks-dir "$TASKS" --evidence-file "$TMP/good.json"

# ---------------------------------------------------------------------------
say "6/8  結案閘門（merge 證據必須齊全）"
expect_blocked "無 merge 證據不可結案" "merge completion evidence" \
  cli task-transition verify-flow closed --tasks-dir "$TASKS" --evidence-file "$TMP/empty.json"

cat > "$TMP/wrong-target.json" <<EOF
{"merge_completed":true,"merge_request":"!1","merge_target_branch":"origin/WRONG",
 "merge_actor":"human","merge_at":"2026-01-02T00:00:00Z"}
EOF
expect_blocked "merge 目標分支不符不可結案" "merge target branch does not match" \
  cli task-transition verify-flow closed --tasks-dir "$TASKS" --evidence-file "$TMP/wrong-target.json"

cat > "$TMP/merged.json" <<EOF
{"merge_completed":true,"merge_request":"!1","merge_target_branch":"$BASE",
 "merge_actor":"human","merge_at":"2026-01-02T00:00:00Z"}
EOF
expect_state "merge 證據齊全才可結案" closed \
  cli task-transition verify-flow closed --tasks-dir "$TASKS" --evidence-file "$TMP/merged.json"

# ---------------------------------------------------------------------------
say "7/8  證據已持久化"
for f in brief.md report.md review.md acceptance.md delivery.md; do
  [ -s "$TASKS/verify-flow/$f" ] && ok "$f 已寫入" || bad "$f 缺漏或空白"
done
grep -q "merge_completed" "$TASKS/verify-flow/delivery.md" && ok "delivery.md 含 merge 證據" || bad "delivery.md 無 merge 證據"

# ---------------------------------------------------------------------------
say "8/8  已結案的任務不可再轉換"
expect_blocked "closed 是終態" "invalid transition" \
  cli task-transition verify-flow active --tasks-dir "$TASKS"

# ---------------------------------------------------------------------------
say "結果"
printf '    通過 %s／失敗 %s\n\n' "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
