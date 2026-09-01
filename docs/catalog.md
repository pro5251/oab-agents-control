# OpenAB Agent Catalog 預檢

`oab-control` 是第一個本機 Control CLI 切片。在呼叫任何 K3s 或 Discord
adapter 前，它會先驗證版本化且不含 Secret 值的 catalog。

```bash
python3 -m oab_control.cli environment-validate config/environment.yaml
python3 -m oab_control.cli validate examples/catalog.example.yaml --no-path-check --json \
  --secrets-file config/reference-manifest.example.yaml
python3 -m oab_control.cli render-openab examples/catalog.example.yaml --no-path-check \
  --secrets-file config/reference-manifest.example.yaml > /tmp/openab-values.yaml
python3 -m oab_control.cli render-k8s examples/catalog.example.yaml --no-path-check \
  --namespace oab-agents-local > /tmp/oab-isolation.yaml
```

驗證器刻意採 fail-closed 原則：

- 必須有一位 leader 與至少一位 worker；
- channel 與 bot ID 必須是唯一的數字 Discord snowflake；
- `allow_all_channels` 與 `allow_all_users` 必須明確為 `false`；
- worker 的 human allowlist 必須為空，且只信任 leader bot ID；
- leader 只能信任指定 worker 的 bot ID；
- worktree 與 container mount 路徑必須是絕對、唯一且互不重疊的路徑；
- 持久 worktree 不可與 source collection root 重疊；
- 精確的 repository grant 必須位於單一 collection root 內，使用安全的
  checkout 子路徑，並預設為 `origin/develop`；
- 只接受 `read` 或 `write` grant，並拒絕未知欄位；
- 每個 agent 必須有不同的 Discord Secret reference；只有操作員記錄
  bootstrap 例外時，GitLab identity reference 才可共用；
- 拒絕 Secret 值與看似憑證的字串；catalog 中只能出現 Secret 與 identity
  reference。

`plan` 會將正規化後的 catalog 投影為每個 agent 一份獨立 workload，內容包括
runtime PVC／ServiceAccount 名稱、逐一 grant 的精確 mount、Secret 與 identity
reference、RBAC 隔離，以及 default-deny NetworkPolicy 意圖。結果會明確標示為
不會變更系統且需要人類確認；它不是 `kubectl apply` 的包裝器。

每份正規化 catalog 都會取得可重現的 12 字元 revision；plan 與產生的
ServiceAccount label 會公開此 revision 供 reconciliation 使用，且不包含任何
Secret 值。

部署前，renderer 會以相鄰的 OpenAB chart 進行驗證：chart 中預設啟用的範例
`kiro` profile 會被明確停用；每個 catalog ID 會成為 Deployment／ConfigMap／PVC
名稱，而 ServiceAccount 則使用 `oab-agent-<id>` 前綴。這可避免上游範例在未察覺
的情況下成為第五個 workload。

worktree manager 會對每個精確 grant 執行完整的 `git clone --no-hardlinks`，
再將 `origin` 設為操作員提供的 delivery remote。它絕不使用 linked worktree，
也不會 mount source collection。請搭配僅含名稱的 JSON remote map 使用
`worktree-materialize`；若缺少 remote、branch 不安全、branch 切換時不乾淨，
或 `origin` 不一致，皆會 fail closed。

任務紀錄存放於 `.oab-control/tasks/<task-id>/` 下。只有設定的 leader 可建立、
轉換或保存 report／review。程式驗收需要 developer 測試、獨立 review、成功的
CI 狀態、leader 摘要、與確切 delivery owner、repository、branch 相符的
commit／push／MR 證據，以及包含 actor、timestamp、scope 的明確人類 merge
授權；研究／文件任務使用 evidence gate，不需要 MR。未完成的任務只有在保存
cancellation reason 與 decider 後才能關閉；不得直接跳至 `closed` 以繞過
acceptance gate。程式任務在 `accepted → closed` 時還必須保存
`merge_completed`、MR、merge actor／timestamp 與等於 `base_branch` 的 merge target
證據；此紀錄會保存於 `delivery.md`。若任務取消（即使已驗收但尚未 merge），則保存
cancellation reason、decider 與取消時間；cleanup 只接受已持久化的 merge 或
cancellation closure。

程式任務於 `accepted` 時，`task.json` 會持久化 commit SHA、push ref、GitLab MR
reference 與 CI status。這些欄位使 Control status 與未來 UI 能顯示仍待人類 merge
的正式交付物；它們不代表 merge 已完成，也不會解除 merge gate。

`status` 會重新驗證每個 task envelope 是否仍符合當前 catalog。當 repository grant、
worktree、base branch、GitLab identity 或 private reply channel 不再相符時，輸出會
標記 `catalog_binding: mismatch`，而仍在執行的任務會以
`needs-reconciliation` 顯示。這不會自動變更 task；leader 必須明確重新協調或取消。

第一期 `TaskStore` 的全域未結束任務上限是 2，且每個 Agent 只能有一項。這與每 Agent
各自長期 worktree 的限制共同避免並行工作超出首次部署的可觀測與協調能力；需要擴容時，
應透過 Control CLI／未來 UI 的同一控制層調整 `max_active_tasks`，不可讓 worker 自行繞過。

提供 `--secrets-file` 時，每一個 `bot_secret_ref` 與 `gitlab_identity_ref`
也都必須出現在該本機的「僅含名稱」manifest。此 manifest 不是憑證儲存區；實際
值必須留在操作員忽略版本控制的 `config/secrets.yaml` 或 Kubernetes Secrets 中。

`--check-git` 會額外檢查本機 `.git` metadata。驗證器不會將本機 source
collection 作為 delivery remote，也不會將任何 collection root mount 到
workload；worktree materialization 與 Kubernetes rendering 則屬後續的 Control
CLI 切片。

## 本機操作

不帶 `--yes` 執行 `deploy` 時，會先驗證並寫入不含 Secret 的預覽 snapshot；
該操作只會回傳 plan，不會變更 Kubernetes。確認執行則需要
`--environment <ready-contract>`、包含 `Chart.yaml` 的明確
`--chart <openab-chart>`，以及三個
外部備份輸入（`--backup-sources`、`--backup-output`、`--backup-attestation`）。
備份必須在建立 snapshot 與任何 Kubernetes／Helm 呼叫前完成；備份失敗會阻擋
部署。只有通過後才會 materialize 本機 Secret 值並呼叫 `kubectl apply`／Helm。
確認執行時，catalog／worktree／repository 與備份來源路徑也必須與 ready 的
環境合約相符；來自其他本機 workspace 的路徑會被拒絕。啟用路徑檢查時（CLI
預設），在產生 Helm hostPath mount 前，每個 agent worktree 與精確 checkout
都必須已 materialize 為獨立 Git checkout。本 repository 的預設環境刻意為
`bootstrap-pending`，因此在操作員填妥 pending decisions 前，不可用於實際套用。

ready 環境合約還必須記錄 K3s context、指向 namespace-scoped deployer kubeconfig
的環境變數、指向獨立 Secret materializer kubeconfig 的環境變數、
`secrets_encryption_enabled: true`、不含 Secret 的 encryption-key recovery
reference，以及 `network_policy_controller: kube-router`。兩個 kubeconfig 環境
變數必須不同；deployer 不能用於寫入 Secret。合約只保存 metadata；key 本身絕不可
進入此 repository。

`status` 是唯讀操作，會回報 catalog plan，以及任何本機 workspace registry 和
task record。提供 registry／task 路徑時，它會加入唯讀的 workspace observation
（`clean`、`dirty`、`missing`），並標記逾期 checkpoint 或
`needs-reconciliation`；在安裝 Discord／worker adapter 前，heartbeat 會維持
`not-configured`。在 Kubernetes observation adapter 可連上 cluster 前，runtime
health 會回報 `unknown`，而不會猜測狀態。
若提供 `status --environment <ready-contract>`，且合約已為 `ready`，則預設
Kubernetes observer 會使用該合約指定的 `deployer_kubeconfig_env`；否則仍可安全地
回報本機狀態與 runtime `unknown`。

`preflight <environment> --chart <openab-chart>` 是部署前的唯讀彙整檢查。它會回報
contract readiness、必要的本機工具、OpenAB chart metadata，以及兩個 kubeconfig
環境變數是否各自指向不同的存在檔案；它不讀取 kubeconfig 內容、不輸出其路徑，亦不
連線 Kubernetes 或執行 Helm。

provider-neutral 的 `oab_control.discord_policy` 模組，是未來 Discord adapter
的 admission seam。它會拒絕錯誤 channel、所有 worker human message、不受信任
的 bot 作者，以及未明確 mention 的 bot message；每個判定都包含不含內容的 audit
event。它的 dispatch helper 只會從 task envelope 解析 worker 已設定的 private
channel，因此 Web UI 或 script 可重用同一 policy，而不會繞過 catalog。
`rollback` 會預覽 snapshot；通過相同的明確確認與 ready-environment 檢查後，
才還原 catalog 與 render 後的 desired state；它不會變更 Git branch 或 merge
request。

`render-k8s` 會產生 bootstrap Namespace、六個 ServiceAccount（四個 agent
account，加上 deployer 與 secret-materializer）、每個 agent 的空 Roles／
RoleBindings，以及 namespace 的 default-deny policy。允許的 proxy endpoint
刻意放在獨立的 `oab-egress` namespace；在選定本機實作前，不會憑空建立 proxy
workload 或 domain-level policy。Namespace 建立與 deployer RBAC 屬於 bootstrap
操作；日常部署時，請將環境合約指定的 `deployer_kubeconfig_env`（通常為
`KUBECONFIG`）指向 namespace-scoped 的 `oab-control-deployer` credential，
且不可用 agent ServiceAccount 執行 CLI。
本機 Secret materialization 則只使用不同的
`secret_materializer_kubeconfig_env`，對應 `oab-control-secret-materializer`
credential。

OpenAB values 會明確固定 Pod／container 安全內容：`runAsNonRoot`、非 root UID/GID、
`RuntimeDefault` seccomp、禁止 privilege escalation、唯讀 root filesystem 與
`drop: [ALL]` capabilities。只有 chart 提供的 runtime PVC、`/tmp` emptyDir 與 catalog
指定的精確 worktree mount 可寫入；不依賴上游 chart 的預設值來維持這些防護。

`worktree-materialize` 預設需要已存在且為 assigned／active 的 task，會使用完整
獨立 clone，並可透過 `--registry-json` 更新 JSON／Markdown workspace registry。
materialize 成功後，會在 agent root 下新增 ownership marker。`worktree-retire`
在刪除前需要 catalog 管理的路徑、該 marker、`--yes`，以及（提供時）既有的
registry row；之後 registry row 會保留並標示為 `retired`。

獨立的 `backup` 命令會將 catalog、coordination repository、agent worktree 與
K3s state 複製至操作員 attest 的外部目標。它需要明確確認與簡短、不含 Secret
的 encryption attestation，記錄 SHA-256 checksum，並排除慣用的本機 Secret-value
檔名。`restore` 會驗證 manifest／checksum，需要確認，且只還原到不存在且乾淨的
目的地；它永不還原本機 Secret-values 檔案。

`config/environment.yaml` 刻意維持 `bootstrap-pending`：repository 不會猜測公司
GitLab project、Discord ID、model／image 選項或 Secret 位置。只有操作員填入這些
決策並將 `status` 改為 `ready` 後，`environment-validate --require-ready` 才應通過。
