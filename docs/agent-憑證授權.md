# Agent LLM 憑證授權

Agent 掛載了 workspace、連上了 Discord，但**沒有 LLM 憑證就無法思考**。
每個 agent 用哪個模型供應商，取決於它的 image 變體
（見 [參考/catalog-契約.md](參考/catalog-契約.md#working_dir-與-image-變體)）。

---

## 每個變體的授權方式

image 內建的環境變數 `OPENAB_AGENT_AUTH_COMMAND` 就是該變體的授權指令：

| 變體 | agent CLI | 授權指令 | 憑證存放 |
| --- | --- | --- | --- |
| `-codex` | `codex-acp` | `codex login --device-auth` | `~/.codex/auth.json` |
| `-copilot` | `copilot --acp --stdio` | `copilot login` | `~/.copilot/` |
| `-claude` | `claude` | `claude`（首次啟動時的 OAuth） | `~/.claude/` |
| `-gemini` | `gemini` | `gemini`（首次啟動時的 OAuth） | `~/.gemini/` |

三者都是 **OAuth device flow**：指令會印出一個網址與代碼，
你在瀏覽器輸入代碼並批准，指令輪詢到 token 後寫入檔案。

---

## 為什麼憑證會保留

chart 把 runtime PVC 掛在 agent 的家目錄（`/home/node` 或 `/home/agent`）。
`~/.codex`、`~/.copilot` 都在那底下，所以：

- **Pod 重啟、重新部署 → 憑證保留**
- 刪除 PVC（`kubectl -n oab-agents delete pvc <agent>`）→ 憑證消失，需重新授權

驗證：codex 的 `auth.json` 建立於某次部署前，
Pod 重啟後 `codex login status` 仍回報 `Logged in` —— 憑證在 PVC 上。

---

## 逐 agent 授權（每個 agent 各自獨立）

每個 agent 是獨立容器、獨立 PVC，因此**各自授權一次**。
可以四個都用同一個帳號，也可以分開。

授權需要互動式終端，所以用 `exec -it`，且必須用 **bootstrap 身分**
（deployer 沒有 `pods/exec`，理由見
[架構說明.md](架構說明.md#為什麼要兩份-kubeconfig)）。

```bash
ADMIN="$HOME/.kube/oab-admin.kubeconfig"

# codex 變體（預設 leader、developer）
KUBECONFIG=$ADMIN kubectl -n oab-agents exec -it deploy/developer -- codex login --device-auth
KUBECONFIG=$ADMIN kubectl -n oab-agents exec -it deploy/leader     -- codex login --device-auth

# copilot 變體（預設 researcher、reviewer）
KUBECONFIG=$ADMIN kubectl -n oab-agents exec -it deploy/researcher -- copilot login
KUBECONFIG=$ADMIN kubectl -n oab-agents exec -it deploy/reviewer   -- copilot login
```

每個指令會顯示類似：

```
To authenticate, visit https://github.com/login/device and enter code XXXX-XXXX
```

在瀏覽器完成後，指令自己會偵測到並結束。

### 授權後 harness 需要重新讀取

`openab` harness 在啟動時 spawn agent 子行程。若授權是在 harness 啟動**之後**
才完成，重啟該 agent 讓它重新 spawn：

```bash
KUBECONFIG=$ADMIN kubectl -n oab-agents rollout restart deploy/developer
```

---

## codex 變體：關閉內建沙箱

codex 用 **bubblewrap** 建立唯讀沙箱來執行模型產生的指令。
但這個容器本身已經是沙箱（唯讀 rootfs、drop ALL、無 SA token、
egress 限制、逐 checkout 掛載），而 bubblewrap 在此環境**無法建立
user namespace**：

```
bwrap: No permissions to create new namespace
```

結果：codex agent 能通過授權、能回應聊天，但**連讀檔都會失敗**。

修正：讓 codex 把沙箱交給容器層。

```bash
bash scripts/configure-codex-sandbox.sh
```

它會掃出 codex 變體的 agent，寫入 `~/.codex/config.toml`：

```toml
sandbox_mode = "danger-full-access"
approval_policy = "never"
```

名稱看起來嚇人，但推理是成立的：**K8s 容器就是那個沙箱**。
codex 能碰到的最壞情況，是寫入它自己那 11 個 checkout
（唯讀角色連這個都不行）。容器層已經擋住其餘一切。

這份 config 與授權 token 都在 PVC 上，重啟保留；刪 PVC 或新增 agent 需重跑。

**copilot 變體不受影響**——copilot 不使用 bubblewrap，本來就能運作。

## 確認授權狀態

```bash
ADMIN="$HOME/.kube/oab-admin.kubeconfig"

# codex
KUBECONFIG=$ADMIN kubectl -n oab-agents exec deploy/developer -- codex login status
#   → "Logged in using ChatGPT"

# copilot（沒有 status 子命令，用實際呼叫測）
KUBECONFIG=$ADMIN kubectl -n oab-agents exec deploy/researcher -- \
  sh -c 'timeout 25 copilot -p "reply OK" 2>&1 | head -1'
#   → 若回覆內容合理即為已授權；若要求登入則否
```

---

## 用環境變數注入（copilot 的無頭方式）

copilot 也接受環境變數中的 token，優先序：
`COPILOT_GITHUB_TOKEN` → `GH_TOKEN` → `GITHUB_TOKEN`。

支援 fine-grained PAT（需 "Copilot Requests" 權限）或 OAuth token，
**不支援** classic PAT（`ghp_`）。

這條路徑可以透過 catalog 的 Secret 機制注入——在 image 變體是 copilot 時，
於 `runtimes.<role>` 加入 `secretEnv`（若 renderer 支援）。
但這等於把長期 token 放進 K8s Secret，與「憑證用 OAuth 而非 API key」的
[安全建議](規格-agent-工作流程.md)相反，僅在自動化必要時採用。

---

## 目前狀態（本機）

| Agent | 變體 | 授權狀態 |
| --- | --- | --- |
| leader | codex | ✅ Logged in using ChatGPT |
| developer | codex | ✅ Logged in using ChatGPT |
| researcher | copilot | ✅ 可運作 |
| reviewer | copilot | ✅ 可運作 |

四個 agent 都能回應訊息、遵守 `AGENTS.md` 的角色規則，且**能實際讀取 workspace**：

- researcher／reviewer（copilot）：`ls` workspace 正常
- leader／developer（codex）：套用沙箱設定後可讀取（`configure-codex-sandbox.sh`）

實測：對 developer 問「這個目錄是什麼專案」，它正確回答
「MoneyIn 系統的 MySQL 資料庫專案」——確實讀了檔案，且未修改任何東西。

---

## 相關文件

- [參考/catalog-契約.md](參考/catalog-契約.md#working_dir-與-image-變體) — image 變體對照
- [本機部署實作.md](本機部署實作.md) — 部署與更新
- [規格-agent-工作流程.md](規格-agent-工作流程.md) — 為何偏好 OAuth 而非 API key
