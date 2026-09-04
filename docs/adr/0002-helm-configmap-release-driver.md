# 0002. Helm release 使用 ConfigMap driver

日期：2026-09-04
狀態：已採用

## 背景

Helm 3 預設把 release 狀態存成 Secret。

但本專案的 `oab-control-deployer` Role **刻意完全不授予 secrets 權限**——
讀取 Secret 值是另一個身分（`oab-control-secret-materializer`）的職責，
而該身分只有 `create`/`update`/`patch`，沒有讀取權限。

結果是 `helm upgrade --install` 必定失敗：

```
Error: ... secrets is forbidden: ...cannot list resource "secrets"
```

## 決策

`_helm_apply` 明確設定 `HELM_DRIVER=configmap`。
Deployer Role 本來就有 configmaps 的完整權限。

## 後果

**得到**

- 部署可行，且不需要放寬 Secret 邊界。
- Release 狀態可用一般 `kubectl get cm` 檢視。

**付出**

- **所有** `helm` 指令都必須帶 `HELM_DRIVER=configmap`，
  包含 `list`、`history`、`uninstall`。忘記帶會得到權限錯誤。
  這是操作上的陷阱，已記載於操作速查。
- ConfigMap 有 1MiB 大小限制。目前 release 遠低於此，
  但 chart 大幅成長時需重新評估。

## 替代方案

**授予 deployer secrets 權限** — 被否決。這會讓日常部署身分
取得讀取 namespace 內**所有** Secret 的能力，包括 agent 的 Discord token。
為了讓 Helm 記錄狀態而交出整個 Secret 邊界，代價完全不成比例。
