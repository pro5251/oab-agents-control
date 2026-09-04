#!/usr/bin/env bash
# ============================================================================
# 把 K3s 管理的持久資料匯出成 operator 自有、可備份的目錄
#
# 為什麼需要這一步：
#   /var/lib/rancher/k3s 整個是 root-only，而且 server/tls 底下有 17 個 PEM
#   私鑰。LocalBackup.verify() 會主動拒絕任何含私鑰的備份，所以那個路徑
#   在設計上就不可能成為合法的備份來源。
#
#   對這個控制平面而言，真正需要還原的 K3s 狀態是 agent 的 PVC 資料
#   （local-path-provisioner 放在 /var/lib/rancher/k3s/storage），
#   它不含任何憑證。這個腳本把它匯出成使用者擁有的副本。
#
# K3s 伺服器本身的憑證與 datastore 屬於「叢集層級備份」，應由 k3s 自己的
# etcd-snapshot 或磁碟層級加密備份處理，不走這條路徑。
#
# 用法：bash scripts/export-k3s-state.sh
# 需在每次 backup／deploy --yes 之前執行，讓匯出保持最新。
# ============================================================================
set -euo pipefail

SRC=/var/lib/rancher/k3s/storage
DEST="${OAB_K3S_STATE_EXPORT:-$HOME/oab-k3s-state}"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()  { printf '    \033[32m✅\033[0m %s\n' "$*"; }
die() { printf '    \033[31m❌ %s\033[0m\n' "$*" >&2; exit 1; }

say "匯出 K3s PVC 資料 → $DEST"
mkdir -p "$DEST"

# docker 內是 root，可以讀取 700 的 storage 目錄。使用者在 docker 群組中，
# 因此不需要 sudo。匯出後立刻改回使用者所有權。
timeout 300 docker run --rm \
  -v "$SRC":/src:ro \
  -v "$DEST":/dest \
  alpine sh -c '
    set -e
    rm -rf /dest/storage
    mkdir -p /dest/storage
    if [ -n "$(ls -A /src 2>/dev/null)" ]; then cp -a /src/. /dest/storage/; fi
    chown -R '"$(id -u):$(id -g)"' /dest
  ' || die "匯出失敗（docker 是否可用？）"

ok "已匯出：$(find "$DEST" -type f 2>/dev/null | wc -l) 個檔案"

# 防呆：備份驗證會拒絕含私鑰的內容，先在這裡擋掉。
if find "$DEST" -name '*.key' -o -name '*.pem' 2>/dev/null | grep -q .; then
  die "匯出內容含有 .key/.pem，備份會被拒絕；請檢查來源"
fi
ok "不含私鑰，可作為合法備份來源"

printf '\n    環境合約的 paths.k3s_state 與 config/backup-sources.json\n'
printf '    都應指向：%s\n\n' "$DEST"
