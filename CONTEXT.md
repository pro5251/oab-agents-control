# 領域術語

這份文件只定義詞彙。設計理由在 [docs/架構說明.md](docs/架構說明.md)，
操作方式在 [docs/操作手冊.md](docs/操作手冊.md)，
行為契約在 [docs/規格-agent-工作流程.md](docs/規格-agent-工作流程.md)。

---

## 身分

### Leader（兩個意思，必須分開）

這個詞在專案裡被混用，是最容易出錯的地方：

- **Leader agent** — 執行於叢集內的一個 OpenAB Pod，角色為 `leader`。
  它在 Discord 上協調其他三個 agent。它**不能**執行 CLI，
  也**不能**寫入任務紀錄。
- **Leader 操作員** — 操作 `oab-control` CLI 的人類。任務紀錄的唯一寫入者，
  驗收與結案的決定者。

`TaskStore(leader_id=...)` 指的是**操作員**，不是 agent。
本文件其他地方一律寫全稱，不單用「leader」。

### Agent

一個具備 Discord 身分、執行於自己 Pod 中的 OpenAB 執行個體。
四個角色：`leader`、`researcher`、`developer`、`reviewer`。

### Worker

`leader` 以外的三個 agent。Worker 只接受 leader agent 的訊息，
拒絕所有人類訊息。

### Bot

Agent 在 Discord 上的身分。一個 agent 對應一個 bot，
`bot_user_id` 是 Discord 的數字 snowflake，不是 agent 的邏輯 ID。

---

## 程式碼位置

這一組詞彙描述同一份程式碼的四個不同副本，混用會導致嚴重誤解。

### Collection root

主機上存放多個 repository 的目錄。它**不會**被掛載進任何 Pod。
Agent 永遠看不到 collection root 本身，只看得到其中被授權的個別 repository。

### Source repository

Collection root 內的一個 git repository。它是**種子**：
`worktree-materialize` 從它 clone 出 agent 的 checkout。
Agent 不會直接接觸它。

### Delivery remote

Agent checkout 的 `origin`。它代表「交付的目的地」，
語意上對應上游 GitLab，實作上目前是本機 bare mirror。
見 [ADR 0001](docs/adr/0001-local-mirror-as-delivery-remote.md)。

### Mirror

Source repository 的本機 bare 副本，扮演 delivery remote 的角色。
它的 `refs/heads/*` 呈現**上游有什麼**，而不是主機上某個開發者
checkout 過什麼。

### Agent worktree

一個 agent 專屬的目錄（`worktree.path`），底下放它所有的 checkout。
每個 agent 各一個，彼此不重疊，也不與任何 collection root 重疊。

### Checkout

Agent worktree 內的一個完整獨立 git clone，對應一個 repository grant。
這是**唯一**被掛載進 Pod 的東西。

「Workspace」是 checkout 在容器內的別名，
用於 agent 面向的文件（`/workspaces/<agent>/<collection>/<repo>`）。
兩者指同一份東西，只是視角不同：主機視角叫 checkout，容器視角叫 workspace。

---

## 授權

### Repository grant

Catalog 中一筆「某 agent 可以存取某個 repository 的某個 checkout，
以何種權限」的宣告。授權的單位是 grant，不是 collection root，
也不是 agent。

### Access

`read` 或 `write`。它決定容器內掛載的旗標，
由核心強制，不是靠 agent 自律。

### Catalog

宣告所有 agent 與其 grant 的版本化文件。它是**唯一授權來源**。
任務紀錄不是第二個授權來源——每次 `status` 都會重新比對。

---

## 任務

### Task record

由 leader 操作員建立的持久紀錄，綁定一個 agent、一個 grant、一個分支。
它記錄意圖與證據，**不授予任何權限**。

### Envelope

Task record 中用於派工的欄位子集。不含任何憑證。

### Brief

任務建立時產生的人類可讀說明，寫給 agent 看。

### Gate（閘門）

狀態轉換的前置條件。缺少必要證據時轉換會失敗，
而不是產生警告後放行。

### 派工（Dispatch）

**這個詞在三個地方有三個意思，本專案只採用第三個：**

- OpenAB 的 `openab-core/src/dispatch.rs` — ACP turn 的批次排程。與本專案無關。
- `discord_policy.dispatch_channel()` — **解析**某個 worker 的目標頻道。
  它只回答「該送到哪」，不送任何東西。
- **派工** — leader 操作員把一份 task record 的路由摘要，
  以 leader bot 的身分送進該 worker 的私有頻道。

單獨寫「派工」時一律指第三個。指第二個時寫「解析派工頻道」。

### 派工訊息（Dispatch message）

送進 worker 私有頻道的那一則 Discord 訊息。內容是**路由摘要**，
不是完整 brief：它提及該 worker 的 bot、標明任務編號、分支與 checkout 邊界。

它刻意不含 task record 的 goal／scope 全文——那些文字可能來自
被注入的來源，不應該由控制平面轉貼進 Discord。詳見
[任務指示檔](#任務指示檔taskmd)。

### 任務指示檔（TASK.md）

`worktree-materialize` 寫進該 task 分支 checkout 根目錄的檔案，
內含完整 brief。Worker 在容器內直接讀它，內容不經過 Discord。

它是 checkout 內的檔案，因此既有的指示檔偵測（`status` 的
`instruction_files`）自然涵蓋它。

---

## 證據

這一組區分是規格的核心。兩者在檔案中長得很像，但可信度完全不同。

### Verified evidence（已驗證證據）

Leader 操作員能從 agent 的 checkout 直接讀出、不需要 agent 配合、
且 agent 無法偽造的事實。例如 commit SHA、分支名稱、
與 base branch 的差異、worktree 是否乾淨。

### Attested evidence（聲稱證據）

只能由某個角色宣稱、無法由控制平面查證的陳述。
例如「測試通過」、「review 是獨立進行的」。

**聲稱證據不得被呈現為已驗證證據。** 兩者在紀錄中必須可區分，
否則日後檢視的人無法判斷哪一項可以回頭查、哪一項只能相信當時的判斷。

---

## 邊界

### Enforced（強制）

由核心、RBAC 或 NetworkPolicy 實現，agent 無法繞過。
例如唯讀掛載、無 `pods/exec`、egress 限制。

### Advisory（勸告）

寫在 `AGENTS.md` 中、依賴模型遵守的指示。
規格必須明確標示哪些規則屬於這一類——
把勸告寫得像強制，是規格最容易犯的錯。

### Accidental（意外）

技術上目前成立、但沒有人設計過的限制。
意外的邊界會在無人察覺時消失，
因此**不得**被當成控制手段記載。
