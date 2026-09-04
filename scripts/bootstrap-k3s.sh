#!/usr/bin/env bash
# ============================================================================
# K3s 一次性 bootstrap
#
# 這是整個流程中唯一需要 sudo 的步驟，因為 /etc/rancher/k3s/k3s.yaml 是
# root-only（0600）。腳本只用 sudo 複製一次 admin kubeconfig，其餘全部以
# 一般使用者身分執行。
#
# 執行後會建立：
#   - namespace oab-agents、6 個 ServiceAccount、RBAC、default-deny NetworkPolicy
#   - ~/.kube/oab-control-deployer.kubeconfig            （namespace-scoped 部署身分）
#   - ~/.kube/oab-control-secret-materializer.kubeconfig （只能寫 Secret，不能讀）
#
# 用法：
#   bash scripts/bootstrap-k3s.sh
#
# 可重複執行（idempotent）。
# ============================================================================
set -euo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NS=oab-agents
KUBE_DIR="$HOME/.kube"
ADMIN="$KUBE_DIR/oab-admin.kubeconfig"
DEPLOYER="$KUBE_DIR/oab-control-deployer.kubeconfig"
MATERIALIZER="$KUBE_DIR/oab-control-secret-materializer.kubeconfig"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()  { printf '    \033[32m✅\033[0m %s\n' "$*"; }
die() { printf '    \033[31m❌ %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
say "1/6  取得 admin kubeconfig（唯一需要 sudo 的步驟）"
mkdir -p "$KUBE_DIR"; chmod 700 "$KUBE_DIR"
if [ -r "$ADMIN" ]; then
  ok "已存在：$ADMIN"
else
  [ -f /etc/rancher/k3s/k3s.yaml ] || die "找不到 /etc/rancher/k3s/k3s.yaml，K3s 是否已安裝？"
  sudo cp /etc/rancher/k3s/k3s.yaml "$ADMIN"
  sudo chown "$(id -u):$(id -g)" "$ADMIN"
  chmod 600 "$ADMIN"
  ok "已複製並設為 0600：$ADMIN"
fi

export KUBECONFIG="$ADMIN"
kubectl cluster-info >/dev/null 2>&1 || die "無法連線 K3s；請確認 systemctl status k3s"
ok "叢集可連線"

# ---------------------------------------------------------------------------
say "2/6  產生並套用 namespace 隔離資源"
cd "$PROJECT"
PYTHONPATH=. python3 -m oab_control.cli render-k8s config/catalog.yaml \
  --namespace "$NS" --no-path-check > /tmp/oab-isolation.yaml
kubectl apply -f /tmp/oab-isolation.yaml
ok "namespace／ServiceAccount／RBAC／NetworkPolicy 已套用"

# ---------------------------------------------------------------------------
say "3/6  建立長期有效的 ServiceAccount token"
for sa in oab-control-deployer oab-control-secret-materializer; do
  kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: ${sa}-token
  namespace: ${NS}
  annotations:
    kubernetes.io/service-account.name: ${sa}
type: kubernetes.io/service-account-token
EOF
done

for sa in oab-control-deployer oab-control-secret-materializer; do
  for _ in $(seq 30); do
    if [ -n "$(kubectl -n "$NS" get secret "${sa}-token" -o jsonpath='{.data.token}' 2>/dev/null)" ]; then break; fi
    sleep 1
  done
  [ -n "$(kubectl -n "$NS" get secret "${sa}-token" -o jsonpath='{.data.token}' 2>/dev/null)" ] \
    || die "${sa}-token 未被填入 token"
  ok "${sa} token 已就緒"
done

# ---------------------------------------------------------------------------
say "4/6  產生兩份 namespace-scoped kubeconfig"
SERVER="$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')"

write_kubeconfig() {
  local sa="$1" out="$2"
  local token ca
  token="$(kubectl -n "$NS" get secret "${sa}-token" -o jsonpath='{.data.token}' | base64 -d)"
  ca="$(kubectl -n "$NS" get secret "${sa}-token" -o jsonpath='{.data.ca\.crt}')"
  umask 077
  cat > "$out" <<EOF
apiVersion: v1
kind: Config
clusters:
  - name: k3s
    cluster:
      server: ${SERVER}
      certificate-authority-data: ${ca}
contexts:
  - name: ${sa}
    context:
      cluster: k3s
      namespace: ${NS}
      user: ${sa}
current-context: ${sa}
users:
  - name: ${sa}
    user:
      token: ${token}
EOF
  chmod 600 "$out"
  ok "$out"
}

write_kubeconfig oab-control-deployer "$DEPLOYER"
write_kubeconfig oab-control-secret-materializer "$MATERIALIZER"

# ---------------------------------------------------------------------------
say "5/6  驗證權限邊界確實生效"
check() { # kubeconfig 動作 資源 期望(yes/no) 說明
  local kc="$1" verb="$2" res="$3" want="$4" desc="$5" got
  got="$(KUBECONFIG="$kc" kubectl auth can-i "$verb" "$res" -n "$NS" 2>/dev/null || true)"
  if [ "$got" = "$want" ]; then ok "$desc（$got）"
  else die "$desc 期望 $want 但得到 $got"; fi
}

# 子資源必須用 --subresource。寫成 `can-i get pods/log` 會被解讀成
# 「取得名為 log 的 pod」而回答 yes，是假陽性——它對 pods/nonexistent
# 也一樣回答 yes。
check_sub() { # kubeconfig 動作 資源 子資源 期望 說明
  local kc="$1" verb="$2" res="$3" sub="$4" want="$5" desc="$6" got
  got="$(KUBECONFIG="$kc" kubectl auth can-i "$verb" "$res" --subresource="$sub" -n "$NS" 2>/dev/null || true)"
  if [ "$got" = "$want" ]; then ok "$desc（$got）"
  else die "$desc 期望 $want 但得到 $got"; fi
}
check "$DEPLOYER"     create deployments     yes "deployer 可建立 Deployment"
check "$DEPLOYER"     create configmaps      yes "deployer 可建立 ConfigMap（Helm release 存放處）"
check "$DEPLOYER"     list   pods            yes "deployer 可觀測 Pod（status 需要）"
check "$DEPLOYER"     list   events          yes "deployer 可讀取事件（診斷需要）"
check "$DEPLOYER"     list   replicasets     yes "deployer 可讀取 ReplicaSet（rollout history 需要）"
check "$DEPLOYER"     get    secrets         no  "deployer 不可讀取 Secret"
check "$DEPLOYER"     delete pods            no  "deployer 不可刪除 Pod"
check "$DEPLOYER"     create pods            no  "deployer 不可直接建立 Pod"
check "$DEPLOYER"     create replicasets     no  "deployer 不可直接建立 ReplicaSet"
# Agent 容器以 secretKeyRef 取得 DISCORD_BOT_TOKEN，因此 exec／log 等同於
# 讀取 Secret。這兩項若被放寬，上面的「不可讀取 Secret」就形同虛設。
check_sub "$DEPLOYER" create pods exec       no  "deployer 不可 exec 進 Pod（會讀到 token）"
check_sub "$DEPLOYER" get    pods log        no  "deployer 不可讀取 Pod 日誌"
check "$MATERIALIZER" create secrets         yes "materializer 可寫入 Secret"
check "$MATERIALIZER" get    secrets         no  "materializer 不可讀回 Secret"
check "$MATERIALIZER" create deployments     no  "materializer 不可部署"

# ---------------------------------------------------------------------------
say "6/6  完成"
cat <<EOF

    在你要執行 deploy 的 shell 中設定這兩個環境變數：

      export KUBECONFIG="$DEPLOYER"
      export OAB_SECRET_MATERIALIZER_KUBECONFIG="$MATERIALIZER"

    要讓它永久生效，可加入 ~/.bashrc。

    下一步：
      cd $PROJECT
      PYTHONPATH=. python3 -m oab_control.cli preflight config/environment.yaml \\
        --chart ~/openab/charts/openab --json

EOF
