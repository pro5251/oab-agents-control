#!/usr/bin/env bash
# Validate the local contract and live Discord/Kubernetes capabilities, then
# deploy the catalog and wait for every agent Deployment to become ready.
#
# This script deliberately refuses to deploy when Discord preflight is not
# green. A deployment cannot repair a Discord channel permission such as 403.
# Override paths with environment variables when running from another clone:
#   DEPLOYER_KUBECONFIG=/path/to/deployer.kubeconfig \
#   MATERIALIZER_KUBECONFIG=/path/to/materializer.kubeconfig \
#   CHART=/path/to/openab/charts/openab \
#   bash scripts/redeploy.sh

set -euo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_HOME="${HOME:?HOME must be set}"
CATALOG="${CATALOG:-$PROJECT/config/catalog.yaml}"
ENVIRONMENT="${ENVIRONMENT:-$PROJECT/config/environment.yaml}"
SECRETS="${SECRETS:-$PROJECT/config/secrets.yaml}"
CHART="${CHART:-$PROJECT/../openab/charts/openab}"
NAMESPACE="${NAMESPACE:-oab-agents}"
RELEASE="${RELEASE:-oab-agents}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-180s}"
DEPLOYER_KUBECONFIG="${DEPLOYER_KUBECONFIG:-$USER_HOME/.kube/oab-control-deployer.kubeconfig}"
MATERIALIZER_KUBECONFIG="${MATERIALIZER_KUBECONFIG:-$USER_HOME/.kube/oab-control-secret-materializer.kubeconfig}"

say() { printf '\n==> %s\n' "$*"; }
ok() { printf '    OK: %s\n' "$*"; }
die() { printf '    ERROR: %s\n' "$*" >&2; exit 1; }

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "找不到必要指令：$1"
}

require_file() {
  [ -f "$1" ] || die "找不到檔案：$1"
}

cd "$PROJECT"
say "1/5 檢查必要工具與輸入"
for command_name in python3 task kubectl; do
  require_command "$command_name"
done
for input_file in "$CATALOG" "$ENVIRONMENT" "$SECRETS" "$DEPLOYER_KUBECONFIG" "$MATERIALIZER_KUBECONFIG"; do
  require_file "$input_file"
done
[ -d "$CHART" ] || die "找不到 OpenAB chart：$CHART"
ok "工具、catalog、environment、secrets、chart 與 kubeconfig 都存在"

# The deployment code reads these exact names from config/environment.yaml.
export KUBECONFIG="$DEPLOYER_KUBECONFIG"
export OAB_SECRET_MATERIALIZER_KUBECONFIG="$MATERIALIZER_KUBECONFIG"

say "2/5 驗證 environment、catalog 與 kubeconfig 權限"
PYTHONPATH=. python3 -m oab_control.cli environment-validate "$ENVIRONMENT" --require-ready
PYTHONPATH=. python3 -m oab_control.cli validate "$CATALOG" --secrets-file "$SECRETS" --check-git

kubectl --kubeconfig="$DEPLOYER_KUBECONFIG" config current-context >/dev/null
auth_can_i() {
  # kubectl auth can-i exits 1 for a valid "no" answer.  Do not let set -e
  # mistake that expected denial for a transport or authentication failure.
  kubectl --kubeconfig="$1" auth can-i "$2" "$3" -n "$NAMESPACE" 2>/dev/null || true
}
DEPLOYMENT_ACCESS="$(auth_can_i "$DEPLOYER_KUBECONFIG" get deployments)"
SECRET_ACCESS="$(auth_can_i "$DEPLOYER_KUBECONFIG" get secrets)"
MATERIALIZER_ACCESS="$(auth_can_i "$MATERIALIZER_KUBECONFIG" patch secrets)"
if [ "$DEPLOYMENT_ACCESS" != "yes" ] || [ "$SECRET_ACCESS" != "no" ] || [ "$MATERIALIZER_ACCESS" != "yes" ]; then
  die "kubeconfig RBAC 不符合部署邊界（deployments=$DEPLOYMENT_ACCESS secrets=$SECRET_ACCESS materializer_secrets=$MATERIALIZER_ACCESS）"
fi
ok "kubeconfig context 與 RBAC 通過"

say "3/5 執行 Discord preflight（未通過就停止，不會部署）"
set +e
DISCORD_PREFLIGHT="$(PYTHONPATH=. python3 -m oab_control.cli discord-preflight --catalog "$CATALOG" --secrets-file "$SECRETS" --json 2>&1)"
DISCORD_RC=$?
set -e
printf '%s\n' "$DISCORD_PREFLIGHT"
if [ "$DISCORD_RC" -ne 0 ]; then
  die "Discord preflight 未通過；請先修正 leader bot 的 worker 頻道權限"
fi
ok "Discord preflight 通過"

say "4/5 套用 Helm/Kubernetes 部署"
PYTHONPATH=. python3 -m oab_control.cli deploy "$CATALOG" \
  --environment "$ENVIRONMENT" \
  --chart "$CHART" \
  --release "$RELEASE" \
  --namespace "$NAMESPACE" \
  --secrets-file "$SECRETS" \
  --snapshot-dir "$PROJECT/.oab-control/snapshots" \
  --yes --json

say "5/5 等待四個 agent rollout"
for agent in leader researcher developer reviewer; do
  kubectl --kubeconfig="$DEPLOYER_KUBECONFIG" -n "$NAMESPACE" \
    rollout status "deployment/$agent" --timeout="$ROLLOUT_TIMEOUT"
done
ok "重新部署完成，所有 agent Deployment 已就緒"
