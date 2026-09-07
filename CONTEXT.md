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

### Delivery task（交付任務）

由 leader 操作員建立的持久工作單位，擁有一個交付 owner、一個 grant 與一個交付分支。
它記錄最終成果與驗收生命週期，**不授予任何權限**。
_避免：用「task」同時指某一次派工。_

### Work assignment（工作指派）

Delivery task 底下交給單一 target agent 的一棒工作；其身分、phase、subject 與路由一經建立便不可改寫。
同一個 task 可以先有 execution assignment，再有一或多個依序產生的 review assignment。
_避免：reassignment、第二個 reviewer task。_

### Review subject（審查標的）

Reviewer 被要求審查的不可變內容識別：code task 使用精確 commit SHA，其他 task 使用控制平面封存內容的 SHA-256。
_避免：branch 最新狀態、未綁版本的「developer 產出」。_

### Review record（審查紀錄）

將 reviewer 身分、review subject、Discord provenance、verdict 與 findings 綁在一起的持久紀錄。
它能證明誰針對哪個版本回報，但不能證明審查品質。

### Assignment envelope

Work assignment 中用於派工的不可變欄位子集。不含任何憑證或 goal／scope 全文。

### Assignment brief

由控制平面從 task 與 assignment 產生、寫給 target agent 的完整說明。
它存放在 repository 之外的唯讀 agent inbox。

### Agent inbox

由控制平面寫入、只對單一 agent 唯讀掛載的 assignment 指示區。
它不是 Git checkout，也不是 agent 可回寫的回報管道。

### Gate（閘門）

狀態轉換的前置條件。缺少必要證據時轉換會失敗，
而不是產生警告後放行。

### 派工（Dispatch）

**這個詞在三個地方有三個意思，本專案只採用第三個：**

- OpenAB 的 `openab-core/src/dispatch.rs` — ACP turn 的批次排程。與本專案無關。
- `discord_policy.dispatch_channel()` — **解析**某個 worker 的目標頻道。
  它只回答「該送到哪」，不送任何東西。
- **派工** — leader 操作員把一份 assignment envelope，
  透過控制平面以 leader bot 的身分送進該 worker 的私有頻道。

單獨寫「派工」時一律指第三個。指第二個時寫「解析派工頻道」。

### 派工訊息（Dispatch message）

送進 worker 私有頻道的那一則 Discord 訊息。內容是**路由摘要**，
不是完整 brief：它提及該 worker 的 bot、標明 task／assignment、review subject 與 inbox 路徑。

它刻意不含 task record 的 goal／scope 全文——那些文字可能來自
被注入的來源，不應該由控制平面轉貼進 Discord。完整內容只進 agent inbox。

### Dispatch attempt（派工嘗試）

控制平面對一個 work assignment 的單次外送嘗試。每次嘗試有唯一 nonce；
`delivered` 只表示 Discord 已接受訊息，逾時且無法判定時是 `unknown`。
_避免：把 HTTP timeout 直接叫作失敗或成功。_

### Acknowledgement（收件確認）

Target agent 以自己的 Discord bot 身分，在該 assignment 的 thread 中留下可關聯回覆。
它與 dispatch、開始工作及完成工作是不同事實。

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
