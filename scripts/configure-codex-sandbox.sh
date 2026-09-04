#!/usr/bin/env bash
# 讓 codex 變體的 agent 停用內建 bubblewrap 沙箱，改由 K8s 容器層強制邊界。
#
# 為什麼：
#   codex 用 bubblewrap 建立唯讀沙箱來執行模型產生的指令。但這個容器已經是
#   沙箱——唯讀 rootfs、drop ALL、無 SA token、egress 限制、逐 checkout 掛載——
#   而 bubblewrap 在此環境無法建立 user namespace（WSL2 核心／securityContext），
#   所以 codex 連讀檔都會失敗：
#     bwrap: No permissions to create new namespace
#
#   把 codex 的沙箱關掉是安全的：它能碰到的最壞情況，就是寫入自己那 11 個
#   checkout（唯讀角色連這個都不行）。容器層已經擋住其餘一切。
#
# 對 copilot 變體無影響（copilot 不使用 bubblewrap，本來就能運作）。
#
# 憑證與這份 config 都在 runtime PVC 上，重啟保留；刪 PVC 或新增 agent 需重跑。
#
# 用法：bash scripts/configure-codex-sandbox.sh
set -uo pipefail

ADMIN="${KUBECONFIG_ADMIN:-$HOME/.kube/oab-admin.kubeconfig}"
NS=oab-agents

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()  { printf '    \033[32m✅\033[0m %s\n' "$*"; }
skip(){ printf '    ·  %s\n' "$*"; }

[ -f "$ADMIN" ] || { echo "找不到 $ADMIN（bootstrap kubeconfig）"; exit 1; }
export KUBECONFIG="$ADMIN"

say "掃描 codex 變體的 agent"
applied=0
for deploy in $(kubectl -n "$NS" get deploy -o name); do
  name="${deploy#deployment.apps/}"
  # 判斷變體：容器內是否有 codex-acp 執行檔
  if kubectl -n "$NS" exec "$deploy" -- sh -c 'command -v codex-acp >/dev/null 2>&1' 2>/dev/null; then
    kubectl -n "$NS" exec "$deploy" -- sh -c 'mkdir -p ~/.codex && cat > ~/.codex/config.toml <<EOF
# 由 scripts/configure-codex-sandbox.sh 寫入。
# 沙箱交給 K8s 容器層——見該腳本的說明。
sandbox_mode = "danger-full-access"
approval_policy = "never"
EOF' 2>/dev/null && ok "$name（codex）已設定" && applied=$((applied+1))
  else
    skip "$name（非 codex 變體，略過）"
  fi
done

say "驗證"
for deploy in $(kubectl -n "$NS" get deploy -o name); do
  name="${deploy#deployment.apps/}"
  kubectl -n "$NS" exec "$deploy" -- sh -c 'command -v codex-acp >/dev/null 2>&1' 2>/dev/null || continue
  agent_dir="/workspaces/${name}"
  first=$(kubectl -n "$NS" exec "$deploy" -- sh -c "ls -d ${agent_dir}/*/* 2>/dev/null | head -1" 2>/dev/null)
  [ -n "$first" ] || continue
  out=$(kubectl -n "$NS" exec "$deploy" -- sh -c "cd '$first' && timeout 35 codex exec --skip-git-repo-check 'reply with only the word READY' 2>&1" 2>/dev/null)
  if printf '%s' "$out" | grep -q "READY"; then
    ok "$name 可執行 codex（讀取 workspace 正常）"
  else
    printf '    \033[31m❌\033[0m %s codex 仍無法執行：%s\n' "$name" "$(printf '%s' "$out" | tail -1 | cut -c1-120)"
  fi
done

say "完成：$applied 個 codex agent 已設定"
echo "    這份 config 在 PVC 上，重啟保留。若之後 rollout restart，harness 會重新"
echo "    spawn codex 子行程並讀到它。"
