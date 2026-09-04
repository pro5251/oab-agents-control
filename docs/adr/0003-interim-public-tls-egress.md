# 0003. Egress 暫時採 public-tls，正解是 Cilium toFQDNs

日期：2026-09-04
狀態：已採用（過渡）

## 背景

原設計的 egress 管控是兩層：NetworkPolicy 只准 agent 連到
`oab-egress` namespace 的 proxy，proxy 再依網域白名單放行。
這個結構是對的——Kubernetes NetworkPolicy 只認 IP 與 CIDR，不認網域，
所以需要一個看得到 CONNECT 目標的元件補上那一層。

但這個設計有一個未被寫下的前提：**client 必須支援 proxy**。
OpenAB 不支援：

- Discord gateway 走 `tokio_tungstenite::connect_async`，直連
- 二進位檔中不存在 `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY` 字串
- 設定檔沒有任何 proxy 欄位

因此 `proxy-only` 模式下，即使把 proxy 建起來，agent 也不會使用它。
實際結果是 agent 完全沒有對外路徑：REST 與 WebSocket 都得到
`ConnectionRefused`，bot 永遠停在 `discord bot running`。

## 決策

環境合約新增 `k3s.egress_mode`，預設 `proxy-only`，
本機採用 `public-tls`：額外放行公網 TCP/443，
但 RFC1918、link-local（含 `169.254.169.254`）、loopback、CGNAT
仍在 `ipBlock.except` 中。

決策記錄在合約內，不以 `kubectl patch` 私下變更，
因此它會出現在版控、渲染結果與 NetworkPolicy 自身的 annotation。

## 後果

**得到**

- Agent 可連線，bot 可運作。
- 叢集內橫向移動仍被阻擋。
- 雲端 metadata 位址仍被阻擋（SSRF 竊取憑證的經典目標）。
- 非 443 埠仍被阻擋。

**付出**

- **沒有逐網域管控**：agent 可連任何公開 HTTPS 主機。
  在掛載真實專案原始碼的情況下，這是明確的外洩風險。

## 何時必須收緊

任一條成立即不再是選配：

1. Agent 取得真實 LLM 憑證（能自主行動）
2. Workspace 掛載不希望外流的內容
3. 系統運行於共用或正式主機

## 替代方案

**建 HTTP proxy** — 被否決，理由見背景。建了也不會被使用。

**Cilium `toFQDNs`** — 這是**正解**，但尚未採用。
Cilium 以 eBPF 攔截 DNS 回應並動態放行對應 IP，
**不需要 client 支援 proxy**，因此涵蓋 proxy 方案漏掉的 WebSocket。
代價是要替換 k3s 預設的 flannel + kube-router，會使叢集短暫中斷。

**維持 proxy-only** — 被否決。它不是「更安全」，
而是讓整個系統無法運作，同時給人已經受保護的錯覺。
