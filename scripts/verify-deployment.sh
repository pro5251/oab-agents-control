#!/usr/bin/env bash
# Read-only verification for the deployed OpenAB agents.
#
# Usage:
#   bash scripts/verify-deployment.sh
#
# Override KUBECONFIG, NAMESPACE, CATALOG, ENVIRONMENT, SECRETS, or TASKS_DIR
# when checking another local contract. No Kubernetes resource is modified.

set -euo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_HOME="${HOME:?HOME must be set}"
NAMESPACE="${NAMESPACE:-oab-agents}"
KUBE_CONFIG="${KUBE_CONFIG:-$USER_HOME/.kube/oab-control-deployer.kubeconfig}"
CATALOG="${CATALOG:-$PROJECT/config/catalog.yaml}"
ENVIRONMENT="${ENVIRONMENT:-$PROJECT/config/environment.yaml}"
SECRETS="${SECRETS:-$PROJECT/config/secrets.yaml}"
TASKS_DIR="${TASKS_DIR:-$PROJECT/.oab-control/tasks}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-30s}"

say() { printf '\n==> %s\n' "$*"; }
ok() { printf '    OK: %s\n' "$*"; }
die() { printf '    ERROR: %s\n' "$*" >&2; exit 1; }

command -v kubectl >/dev/null 2>&1 || die "找不到 kubectl"
command -v python3 >/dev/null 2>&1 || die "找不到 python3"
[ -f "$KUBE_CONFIG" ] || die "找不到 kubeconfig：$KUBE_CONFIG"

export KUBECONFIG="$KUBE_CONFIG"
cd "$PROJECT"

say "1/4 檢查 Kubernetes context"
CONTEXT="$(kubectl config current-context)"
[ -n "$CONTEXT" ] || die "目前沒有 Kubernetes context"
printf '    context: %s\n' "$CONTEXT"

say "2/4 等待四個 agent rollout"
for agent in leader researcher developer reviewer; do
  kubectl -n "$NAMESPACE" rollout status "deployment/$agent" --timeout="$ROLLOUT_TIMEOUT"
done
ok "四個 agent Deployment rollout 正常"

say "3/4 檢查 Deployment、Pod 與 image"
kubectl -n "$NAMESPACE" get deployments
kubectl -n "$NAMESPACE" get pods -o wide
kubectl -n "$NAMESPACE" get deployment \
  leader researcher developer reviewer \
  -o custom-columns='AGENT:.metadata.name,IMAGE:.spec.template.spec.containers[0].image,GEN:.metadata.generation,OBSERVED:.status.observedGeneration,UPDATED:.status.updatedReplicas,READY:.status.readyReplicas,AVAILABLE:.status.availableReplicas'

say "4/4 檢查控制平面 status"
PYTHONPATH=. python3 -m oab_control.cli status \
  "$CATALOG" \
  --environment "$ENVIRONMENT" \
  --secrets-file "$SECRETS" \
  --tasks-dir "$TASKS_DIR" \
  --json

ok "部署驗證完成；此腳本只讀，未修改 Kubernetes"
