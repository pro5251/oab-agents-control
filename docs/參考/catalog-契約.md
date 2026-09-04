# Catalog 契約參考

`catalog.yaml` 的欄位定義與驗證規則。這是查閱用的參考文件 ——
操作流程請見 [操作手冊.md](../操作手冊.md)，設計理由請見
[架構說明.md](../架構說明.md)。

驗證器（`oab_control.catalog`）沒有 Kubernetes、Discord 或 GitLab client，
因此可以在叢集或外部憑證存在之前執行。

```bash
PYTHONPATH=. python3 -m oab_control.cli validate config/catalog.yaml \
  --secrets-file config/reference-manifest.yaml --json
```

可執行的範例見 [`examples/catalog.example.yaml`](../../examples/catalog.example.yaml)。

---

## 頂層結構

```yaml
version: 1
defaults: { ... }
agents:
  <agent-id>: { ... }
```

| 欄位 | 必填 | 規則 |
| --- | --- | --- |
| `version` | 是 | 必須等於 `1` |
| `defaults` | 否 | 省略時使用內建預設 |
| `agents` | 是 | 至少一個 leader 與一個 worker |

未知的頂層欄位會被拒絕（`unknown_field`）。

### 拓撲要求

- **恰好一個** `leader`
- **至少一個** worker（`researcher`／`developer`／`reviewer`）

---

## `defaults`

| 欄位 | 預設 | 規則 |
| --- | --- | --- |
| `base_branch` | `origin/develop` | 字串 |
| `human_access` | `deny` | **必須**是 `deny` |
| `bot_message_mode` | `mentions` | **必須**是 `mentions` |

---

## `agents.<agent-id>`

Agent ID 必須符合 `^[a-z][a-z0-9-]{0,62}$`（小寫 DNS 風格識別碼）。

| 欄位 | 必填 | 說明 |
| --- | --- | --- |
| `role` | 是 | `leader`／`researcher`／`developer`／`reviewer` |
| `runtime` | 是 | 執行環境 |
| `discord` | 是 | Discord 身分與准入政策 |
| `worktree` | 是 | 本機工作區與容器掛載點 |
| `repository_grants` | 是 | 精確的 repository 授權清單 |
| `delivery` | 是 | GitLab identity 參照 |
| `egress_grants` | 是 | 具名的網路出口授權 |
| `resources` | 是 | CPU／記憶體 requests 與 limits |

### `runtime`

| 欄位 | 必填 | 規則 |
| --- | --- | --- |
| `command` | 是 | 單一執行檔名稱或路徑，僅允許 `[A-Za-z0-9._/+:-]`。**不可含 shell 語法**，旗標請放 `args` |
| `args` | 否 | 字串陣列，不可含控制字元 |
| `model` | 是 | 字串，不可含控制字元 |
| `image` | 是 | 容器映像檔，不可含控制字元 |
| `working_dir` | 否 | 絕對路徑，預設 `/home/agent`。**必須等於該 image 內使用者的 home** |

### `working_dir` 與 image 變體

OpenAB 的發佈映像檔依內建的 ACP CLI 分成不同變體，**使用者與 home 並不一致**：

| 變體 | user | uid | home | `command` 應填 |
| --- | --- | --- | --- | --- |
| `-native` | `agent` | 1000 | `/home/agent` | `openab-agent` |
| `latest`（kiro） | `agent` | 1000 | `/home/agent` | `kiro-cli` |
| `-claude` | `node` | 1000 | `/home/node` | `claude` |
| `-gemini` | `node` | 1000 | `/home/node` | `gemini` |
| `-opencode` | `node` | 1000 | `/home/node` | `opencode` |

**所有變體的 UID 都是 1000**（實測 `-native` 與 `-claude` 皆為 `uid=1000`），
只有使用者名稱與 home 路徑不同。因此 renderer 固定的
`runAsUser: 1000`／`fsGroup: 1000` 對所有變體都成立，
**唯一需要跟著變體調整的就是 `working_dir`**。

chart 會把 runtime PVC 掛在 `working_dir`。若它與 image 的實際 home 不符，
容器就會在唯讀 root filesystem 上沒有可寫的家目錄。

`working_dir` 是**逐 agent** 設定，因此不同 agent 可以使用不同變體：

```yaml
developer:
  runtime:
    command: claude
    args: []
    model: claude-sonnet-4-5
    image: ghcr.io/openabdev/openab:0.9.0-beta.9-claude
    working_dir: /home/node        # 這個變體以 node 身分執行

reviewer:
  runtime:
    command: openab-agent
    args: []
    model: model-reviewer
    image: ghcr.io/openabdev/openab:0.9.0-beta.9-native
    # 省略 working_dir，沿用預設 /home/agent
```

確認某個變體的實際 home：

```bash
docker run --rm --entrypoint sh ghcr.io/openabdev/openab:<tag> -c 'id; echo $HOME'
```

### `discord`

| 欄位 | 必填 | 規則 |
| --- | --- | --- |
| `bot_secret_ref` | 是 | `secret-name/key` 格式；**每個 agent 必須不同** |
| `bot_user_id` | 是 | 17–20 位數字 snowflake；全 catalog 唯一 |
| `entry_channel_id` | leader 專用 | 數字 snowflake。leader **必填**，worker **不可有** |
| `work_channel_id` | worker 專用 | 數字 snowflake。worker **必填**，leader **不可有** |
| `allow_all_channels` | 是 | **必須**明確為 `false` |
| `allow_all_users` | 是 | **必須**明確為 `false` |
| `allowed_users` | 是 | 數字 user ID 陣列。leader **必須非空**，worker **必須為空** |
| `allow_bot_messages` | 是 | **必須**是 `mentions` |
| `allow_user_messages` | 是 | **必須**是 `multibot-mentions` |
| `trusted_bot_ids` | 是 | 見下方信任拓撲 |

**信任拓撲**（跨 agent 檢查）：

- leader 的 `trusted_bot_ids` 必須**恰好等於**所有 worker 的 `bot_user_id` 集合
- 每個 worker 的 `trusted_bot_ids` 必須**只含** leader 的 `bot_user_id`

所有 channel ID 在整份 catalog 中必須唯一。

### `worktree`

| 欄位 | 必填 | 規則 |
| --- | --- | --- |
| `path` | 是 | 絕對路徑。跨 agent 不可重疊 |
| `container_mount_path` | 是 | 絕對路徑。跨 agent 不可重疊 |
| `collection_roots` | 是 | 絕對路徑陣列，彼此不可重疊 |

**關鍵限制**：`path`（持久 worktree）不可與任何 `collection_roots` 重疊。
比較時會同時檢查字面路徑與 `resolve()` 後的真實路徑，
因此 symlink 無法用來繞過此限制。

啟用路徑檢查時，每個 collection root 都必須實際存在。

### `repository_grants[]`

| 欄位 | 必填 | 規則 |
| --- | --- | --- |
| `repository` | 是 | 絕對路徑，必須屬於**恰好一個** collection root |
| `checkout_subpath` | 是 | 相對路徑，不可含 `.`、`..` 或前導 `/` |
| `access` | 是 | `read` 或 `write` |
| `base_branch` | 否 | 預設取自 `defaults.base_branch` |

同一 agent 內，`(repository, checkout_subpath)` 不可重複，
且 checkout 路徑之間不可有前綴包含關係（`a/b` 與 `a/b/c` 視為重疊）。

`access: read` 會 render 成唯讀掛載。

### `delivery`

| 欄位 | 必填 | 規則 |
| --- | --- | --- |
| `gitlab_identity_ref` | 是 | 非密鑰的身分參照名稱，不可含 `/` 或 `\` |

與 `bot_secret_ref` 不同，identity reference **可以**跨 agent 共用
（通常用於 bootstrap 階段）。

### `egress_grants`

具名授權的字串陣列，每項須符合 `^[a-z][a-z0-9-]{0,62}$`。

**不接受萬用字元 `*`。** 空陣列會產生錯誤，
提醒操作員這應該是經過政策審查後的明確決定。

### `resources`

```yaml
resources:
  requests: {cpu: 100m, memory: 256Mi}
  limits:   {cpu: "1",  memory: 1Gi}
```

`requests` 與 `limits` 都必填，且各自都必須同時有 `cpu` 與 `memory`。
數值須符合 Kubernetes quantity 格式（`^[0-9]+(\.[0-9]+)?(m|Ki|Mi|Gi|Ti|Pi|Ei)?$`）。

---

## 密鑰防護

以下情況一律拒絕，出現在**任何**字串欄位皆然：

| 特徵 | 範例 |
| --- | --- |
| OpenAI 風格 token | `sk-...` |
| GitLab PAT | `glpat-...` |
| GitHub token | `ghp_`／`gho_`／`ghu_`／`ghs_`／`ghr_` |
| Slack token | `xoxb-`／`xoxp-`／... |
| URL 內嵌帳密 | `https://user:pass@host` |
| Discord bot token | `Bot <token>` |
| PEM 私鑰 | `-----BEGIN ... PRIVATE KEY-----` |
| 賦值語法 | `token:`／`password=`／`secret:` |

路徑欄位另外拒絕環境變數（`$VAR`、`${VAR}`）、家目錄展開（`~`）、
glob（`*`、`?`、`[]`）、反斜線與 `.`／`..` 片段。

YAML 層級還會拒絕重複的 mapping key。

---

## `--secrets-file`：參照 manifest

提供 `--secrets-file` 時，catalog 中每個 `bot_secret_ref` 與 `gitlab_identity_ref`
都必須出現在該 manifest 中，否則回報 `unresolved_ref`。

```yaml
# config/reference-manifest.yaml —— 只有名稱，沒有值
secret_refs:
  - discord-leader/token
  - discord-developer/token
identity_refs:
  - gitlab-bootstrap
```

允許的頂層欄位只有 `secret_refs`、`identity_refs`、`secrets`。

**這不是憑證儲存區。** 實際值必須留在忽略版控的 `config/secrets.yaml`
或 Kubernetes Secret 中。

---

## CLI 旗標對驗證的影響

| 旗標 | 效果 |
| --- | --- |
| `--no-path-check` | 跳過本機路徑存在性檢查（結構與安全檢查仍會執行） |
| `--check-git` | 額外要求每個 repository 含有 `.git` metadata |
| `--secrets-file <path>` | 啟用參照解析檢查 |
| `--json` | 輸出機器可讀的診斷 |

---

## 正規化輸出

驗證通過後會產生穩定排序的正規化 catalog：

- `allowed_users`、`trusted_bot_ids`、`egress_grants` 去重並排序
- `repository_grants` 依 `(repository, checkout_subpath)` 排序
- 路徑轉為 `resolve()` 後的絕對路徑
- 布林政策欄位固定為 `false`／`mentions`／`multibot-mentions`

```bash
PYTHONPATH=. python3 -m oab_control.cli normalize config/catalog.yaml
```

### Catalog revision

每份正規化 catalog 會得到一個確定性的 12 字元 SHA-256 前綴。
它出現在 plan 與產生的 ServiceAccount label 上，供 reconciliation 使用，
且不包含任何密鑰值。

---

## 產出物契約

### `plan`

將正規化 catalog 投影為每個 agent 一份獨立 workload，包含 runtime PVC／
ServiceAccount 名稱、逐一 grant 的精確掛載、Secret 與 identity 參照、
RBAC 隔離與 default-deny NetworkPolicy 意圖。

結果明確標示 `"mutates_cluster": false` 與 `"requires_human_confirmation": true`。
**它不是 `kubectl apply` 的包裝器。**

### `render-openab`

產生上游 `charts/openab` 可消費的 Helm values，不含 Secret 值。

- chart 內預設啟用的範例 `kiro` profile 會被**明確停用**，
  避免它在未察覺的情況下成為第五個 workload
- 每個 catalog ID 成為 Deployment／ConfigMap／PVC 名稱
- ServiceAccount 使用 `oab-agent-<id>` 前綴
- Pod／container 安全內容固定為：`runAsNonRoot`、非 root UID/GID、
  `RuntimeDefault` seccomp、禁止 privilege escalation、唯讀 root filesystem、
  `capabilities.drop: [ALL]`

只有 chart 提供的 runtime PVC、`/tmp` emptyDir 與 catalog 指定的精確 worktree
掛載可寫入。這些防護不依賴上游 chart 的預設值。

### `render-k8s`

產生 bootstrap Namespace、六個 ServiceAccount（四個 agent，加上 deployer 與
secret-materializer）、每個 agent 的**空** Role／RoleBinding，
以及 namespace 的 default-deny NetworkPolicy。

Agent 的 Role 規則刻意是空的 —— worktree 存取是檔案系統掛載，不是 RBAC 授權。
Agent ServiceAccount 一律 `automountServiceAccountToken: false`。

允許的 proxy endpoint 放在獨立的 `oab-egress` namespace；
在選定本機實作前，不會憑空建立 proxy workload 或 domain-level policy。

---

## 診斷代碼

驗證失敗時，每個診斷包含 `path`、`code` 與 `message`。常見代碼：

| 代碼 | 意義 |
| --- | --- |
| `required` | 必填欄位缺漏 |
| `unknown_field` | 欄位不屬於 catalog version 1 |
| `type` / `key_type` | 型別錯誤 |
| `identifier` | 不符合識別碼語法 |
| `discord_id` | 不是數字 snowflake（常見於誤用邏輯 agent ID） |
| `secret_ref` / `unresolved_ref` | 參照格式錯誤或不在 manifest 中 |
| `duplicate_secret_ref` / `duplicate_bot` / `duplicate_channel` | 跨 agent 重複 |
| `secret_value` | 偵測到憑證特徵字串 |
| `unsafe_path` / `absolute_path` / `unsafe_subpath` | 路徑不安全或非絕對 |
| `path_overlap` / `source_boundary` | 路徑重疊或 worktree 與 collection root 衝突 |
| `collection_boundary` | repository 不屬於恰好一個 collection root |
| `default_deny` / `human_access` / `bot_policy` / `user_policy` | 政策欄位不符合 fail-closed 要求 |
| `trust_topology` | leader／worker 信任關係不正確 |
| `quantity` | 資源數值不是合法的 Kubernetes quantity |
| `egress_grant` | 出口授權是萬用字元或格式錯誤 |
