#!/usr/bin/env bash
# ============================================================================
# 為每個授權的來源 repo 建立／更新本機 bare mirror，作為 delivery remote
#
# 為什麼需要：
#   worktree-materialize 會把 checkout 的 origin 指向 delivery remote 並執行
#   fetch。指向公司 GitLab 需要把私鑰複製進 WSL，且會讓 agent 的 checkout
#   具備往公司 repo push 的管道。改用本機 mirror 則兩者都不需要 ——
#   agent 完全碰不到上游 GitLab。
#
#   代價：上游有更新時要重跑這個腳本同步。
#
# 清單來源：
#   scripts/generate-catalog.py 的 GRANTS 表（同一張表也產生 catalog 與
#   remotes.json）。這個腳本不自己維護清單——先前那樣做的結果是
#   openab-council-relay 被 mirror 到扁平路徑，與產生的 remote 對不上。
#
# 用法：
#   bash scripts/mirror-sources.sh          # 建立或更新全部
#   bash scripts/mirror-sources.sh csp      # 只處理路徑含 csp 的
# ============================================================================
set -uo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FILTER="${1:-}"

# `git clone --mirror` 從「非 bare」來源複製時，refs/heads/* 只會是那台機器上
# 開發者本機有的分支；上游其他分支只以 refs/remotes/origin/* 的形式被帶過來。
# 於是 `git fetch origin master` 會失敗——mirror 沒有 refs/heads/master。
#
# 但 mirror 是在代替上游 GitLab，它的 heads 應該呈現「上游有什麼」，不是
# 「這台機器 checkout 過什麼」。所以把 origin 的每個 ref 提升為 head，
# 並讓 HEAD 指向上游宣告的預設分支。
promote_upstream_refs() {
  local dest="$1" name sha up
  git -C "$dest" for-each-ref --format='%(refname:strip=3)%09%(objectname)' refs/remotes/origin |
    while IFS=$'\t' read -r name sha; do
      [ -n "$name" ] && [ "$name" != "HEAD" ] || continue
      git -C "$dest" update-ref "refs/heads/$name" "$sha" 2>/dev/null
    done
  up="$(git -C "$dest" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||')"
  if [ -n "$up" ] && git -C "$dest" rev-parse --verify --quiet "refs/heads/$up" >/dev/null; then
    git -C "$dest" symbolic-ref HEAD "refs/heads/$up" 2>/dev/null
  fi
}

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()  { printf '    \033[32m✅\033[0m %s\n' "$*"; }
warn(){ printf '    \033[33m⚠️\033[0m  %s\n' "$*"; }
err() { printf '    \033[31m❌\033[0m %s\n' "$*"; }

say "讀取授權清單"
SOURCES="$(python3 "$PROJECT/scripts/generate-catalog.py" --sources)" || {
  err "無法產生來源清單"; exit 1; }
total=$(printf '%s\n' "$SOURCES" | grep -c . )
ok "$total 個 repo"

failed=0
done_count=0
while IFS=$'\t' read -r src dest; do
  [ -n "$src" ] || continue
  [ -n "$FILTER" ] && [[ "$src" != *"$FILTER"* ]] && continue

  # 顯示用短名：去掉共同前綴，避免輸出一長串絕對路徑
  name="$(basename "$(dirname "$src")")/$(basename "$src")"

  if [ ! -e "$src/.git" ]; then
    err "$name — 來源不是 git repo"
    failed=$((failed + 1)); continue
  fi

  mkdir -p "$(dirname "$dest")"
  if [ -d "$dest" ]; then
    if git -C "$dest" remote update --prune >/dev/null 2>&1; then
      promote_upstream_refs "$dest"
      ok "$name — 已同步"
    else
      err "$name — 同步失敗"; failed=$((failed + 1)); continue
    fi
  else
    printf '    ⏳ %s — clone 中…\n' "$name"
    if git clone --mirror "$src" "$dest" >/dev/null 2>&1; then
      promote_upstream_refs "$dest"
      size=$(du -sh "$dest" 2>/dev/null | cut -f1)
      head=$(git -C "$dest" symbolic-ref --short HEAD 2>/dev/null)
      ok "$name — 已建立（${size:-?}，HEAD=${head:-?}）"
    else
      err "$name — clone 失敗"; failed=$((failed + 1)); continue
    fi
  fi
  done_count=$((done_count + 1))
done <<< "$SOURCES"

say "完成：$done_count 成功，$failed 失敗"
[ "$failed" -gt 0 ] && exit 1
ok "全部 mirror 就緒；delivery remote 見 config/remotes.json"
