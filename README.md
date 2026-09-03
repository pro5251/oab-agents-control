# oab-agents-control

OpenAB 多 Agent 架構的 Local-first Control CLI 基礎元件。
它會驗證嚴格的 catalog、建立彼此獨立的 Git checkout、產生上游 OpenAB
values、保存由 leader 管理的任務紀錄，並產生具防護機制的部署計畫。離線
操作不依賴正在運作的 K3s 叢集、Discord、GitLab 或外部控制平面。

執行測試：

macOS／Linux（Bash）：

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

Windows PowerShell：

```powershell
$env:PYTHONPATH = "."
python -m unittest discover -s tests -v
```

PowerShell 不支援 `PYTHONPATH=. command` 這種 Bash 的行內環境變數語法；
PowerShell 請先設定 `$env:PYTHONPATH`。若 Windows 未將 `python` 加入 PATH，
可改用 `py -m unittest discover -s tests -v`。

實用的離線命令：

```bash
PYTHONPATH=. python3 -m oab_control.cli validate examples/catalog.example.yaml \
  --no-path-check --secrets-file config/reference-manifest.example.yaml --json
PYTHONPATH=. python3 -m oab_control.cli plan examples/catalog.example.yaml \
  --no-path-check --secrets-file config/reference-manifest.example.yaml
PYTHONPATH=. python3 -m oab_control.cli render-openab examples/catalog.example.yaml \
  --no-path-check --secrets-file config/reference-manifest.example.yaml > /tmp/openab-values.yaml
```

`deploy --yes` 另外需要 ready 的環境合約、明確的 OpenAB chart 路徑、外部
備份來源／輸出／attestation，以及操作員確認；備份失敗會阻擋
Kubernetes／Helm。`rollback --yes` 需要 ready 環境、chart 路徑與確認。
未通過這些 gate 時，兩者只會建立／顯示快照與計畫。合約與 bootstrap
邊界請參閱 [docs/catalog.md](docs/catalog.md)。若要實際套用，
ready 合約所指定的 `deployer_kubeconfig_env`（通常為 `KUBECONFIG`）必須指向
namespace-scoped deployer 憑證；CLI 拒絕使用未明確指定的管理員 kubeconfig。
提供本機 Secret 值檔時，還必須設定不同的
`secret_materializer_kubeconfig_env`；該憑證只用於套用 Secret，不會用於
Kubernetes isolation 或 Helm。

在執行確認部署前，可先執行唯讀 preflight。它會檢查 ready contract、`git`／
`helm`／`kubectl`、OpenAB chart，以及兩組不同且存在的 kubeconfig 檔案；不會連線、
部署或輸出 kubeconfig 的路徑／內容：

```bash
PYTHONPATH=. python3 -m oab_control.cli preflight config/environment.yaml \
  --chart /path/to/openab/charts/openab --json
```

逐步的 local 操作流程請參閱
[docs/operator-runbook.md](docs/operator-runbook.md)。
若要將此 repository 上傳到 GitHub，請參閱
[Git 遠端與憑證配置手冊](docs/git-遠端與憑證配置手冊.md)。
啟用真實環境前需由操作員提供的資料與權限分界請參閱
[部署前置資料清單](docs/部署前置資料清單.md)。

若要執行實際任務，請先建立由 leader 管理的任務紀錄，將狀態轉為
`assigned`／`active`，再使用操作員提供的僅含名稱之 remote map 建立 Agent
checkout：

```bash
PYTHONPATH=. python3 -m oab_control.cli task-create examples/task.example.json \
  --catalog config/catalog.yaml \
  --secrets-file config/reference-manifest.yaml \
  --tasks-dir .oab-control/tasks --json
PYTHONPATH=. python3 -m oab_control.cli worktree-materialize \
  config/catalog.yaml developer task-001 \
  --remotes-file examples/remotes.example.json \
  --registry-json .oab-control/workspace-registry.json --json
```

第一期 task store 預設最多保留兩項未結束任務，且每個 Agent 同時只能擁有一項；
`planned`、`assigned`、`active`、`review`、`accepted` 與 `needs-reconciliation` 都會佔用此容量。
程式內嵌使用時可透過 `TaskStore(..., max_active_tasks=N)` 調整上限，但 CLI 的一期
預設值固定為 2。

程式任務若完成交付，必須保存 GitLab MR 已 merge 至 `base_branch` 的證據後才能
進入 `closed`；若取消（即使已驗收但尚未 merge），則必須保存 cancellation reason、
decider 與系統記錄的時間。兩種已持久化
的 closure 都可執行 `worktree-cleanup --yes`，移除未追蹤檔案與任務分支並保留
Agent worktree。Agent 退役則是另一個獨立操作，會先驗證 catalog 中的路徑，再執行
`worktree-retire --yes`。

程式任務進入 `accepted` 時，`task.json` 也會保存已驗證的 commit SHA、push ref、
GitLab MR reference 與 CI status；因此 `task-list`／`status --json` 可在尚未 merge
前呈現正式交付物，而 merge completion 仍須另行保存並由人類授權。

`status --tasks-dir ...` 也會將每筆任務 envelope 重新與目前 catalog 比對。若任務的
repository grant、worktree、branch、GitLab identity 或 private Discord channel 已被
修改，觀測結果會標記 `catalog_binding: mismatch`；仍在執行的任務會顯示
`needs-reconciliation`，不會把舊 task record 當成持續授權。

## 目前邊界

此 repository 中的控制切片不宣稱環境已可部署。實際操作員仍須提供並驗證：

- K3s 安裝與 Secret-at-rest 加密
- namespace-scoped deployer RBAC
- local egress proxy 與 domain policy
- Discord 資源權限與 dispatch adapter
- GitLab MR 與 CI evidence adapter
- 外部加密備份／還原演練
- 單一低風險的 end-to-end tracer bullet

上述工作在設計 repository 中以 A001、A003、A005、A007、A009、A011、A012、
A013 與 A014 追蹤。
