# GitLab Personal Access Token（PAT）存放手冊

本手冊適用於在 WSL 透過 HTTPS 存取 GitLab repository 的操作員。
PAT 是憑證；不得寫入 catalog、environment、`identity_refs`、
`config/reference-manifest.yaml`、`config/secrets.yaml`、Git remote URL 或 commit。

## 先釐清兩種資料

| 資料 | 範例 | 存放位置 |
| --- | --- | --- |
| 身分 reference | `gitlab-bootstrap` | catalog 與 names-only reference manifest |
| 真實 PAT | `glpat-...` | Git credential helper 或外部 Secret 管理系統 |

`identity_refs` 只讓 control CLI 驗證 catalog 所引用的 GitLab 身分名稱已獲允許，
不會保存、讀取或登入 PAT。

```yaml
# config/reference-manifest.yaml：可提交，不能包含 PAT
identity_refs:
  - gitlab-bootstrap
```

```yaml
# catalog 的 delivery 區段：只引用名稱
gitlab_identity_ref: gitlab-bootstrap
```

## 建立 PAT

在 GitLab 的 **User settings → Access tokens** 建立 PAT，設定到期日並採用最小權限：

- 僅 clone／fetch：`read_repository`
- 需要 push：`write_repository`

建立後立刻存入下方選定的 credential helper；GitLab 不會再次顯示同一個 PAT。
詳細 scope 與撤銷方式請參閱 [GitLab 官方 PAT 文件](https://docs.gitlab.com/user/profile/personal_access_tokens/)。

## WSL：短期安全快取（建議先使用）

此方式只在記憶體保存 PAT，預設 15 分鐘後失效，不會寫入磁碟。

```bash
git config --global credential.helper 'cache --timeout=900'
```

確認 remote 使用 HTTPS，而不是把 token 放在 URL：

```bash
git remote set-url origin https://gitlab.com/<群組>/<專案>.git
git ls-remote origin
```

第一次連線時輸入：

- `Username`：GitLab 使用者名稱
- `Password`：PAT

要立即清除記憶體中的快取：

```bash
git credential-cache exit
```

## WSL：持久化保存（建議）

若你的電腦是 Windows + WSL，建議使用 **Git for Windows 內建的 Git Credential
Manager（GCM）**。PAT 會加密保存在 Windows Credential Manager，Windows 與 WSL
可共用，不需要把 PAT 留在 WSL 的純文字檔案。

1. 在 Windows 安裝 Git for Windows，並保留預設的 Git Credential Manager 選項。
2. 在 WSL 確認 GCM 存在：

   ```bash
   test -x "/mnt/c/Program Files/Git/mingw64/bin/git-credential-manager.exe"
   ```

   若失敗，請在 Windows 找到 `git-credential-manager.exe` 的實際位置，再以下列
   設定中的路徑取代。
3. 在 WSL 移除短期快取 helper，改用 Windows GCM：

   ```bash
   git config --global --unset-all credential.helper
   git config --global credential.helper '/mnt/c/Program\ Files/Git/mingw64/bin/git-credential-manager.exe'
   ```

4. 確認 remote 使用 HTTPS 後，執行一次：

   ```bash
   git ls-remote origin
   ```

   依提示輸入 GitLab 使用者名稱與 PAT。GCM 會將它持久化保存於 Windows
   Credential Manager；之後從 WSL 或 Windows Git 操作同一 GitLab host 時，都可使用
   該憑證。

官方 WSL 設定路徑請參閱 [GCM 的 WSL 文件](https://github.com/git-ecosystem/git-credential-manager/blob/main/docs/wsl.md)。

### 僅在 WSL 保存的替代方案

若不想使用 Windows 的 Credential Manager，則在 WSL 安裝 GCM，搭配 GPG/`pass`
加密 store。GCM 在 Linux 不會預設選擇 store，需完成下列設定：

```bash
git-credential-manager configure
gpg --gen-key
pass init <你的-GPG-key-ID>
git config --global credential.credentialStore gpg
```

另在 `~/.bashrc` 加入 `export GPG_TTY=$(tty)`，以便純終端機環境輸入 GPG passphrase。
GCM 的安裝方式與 GPG store 條件請參閱 [GCM 安裝文件](https://github.com/git-ecosystem/git-credential-manager/blob/main/docs/install.md) 與 [credential store 文件](https://github.com/git-ecosystem/git-credential-manager/blob/main/docs/credstores.md)。

## 是否應存入 K3s Secret？

**預設不應將操作員的個人 GitLab PAT 存入 K3s。** 若 Git 操作由 WSL 上的
操作員或 control CLI 執行，請使用前述的 GCM 持久化保存方式。

只有在某個 Kubernetes Pod 必須自行 clone、fetch 或 push GitLab repository 時，才應
以 K3s Secret 提供憑證。此時不要重用個人 PAT；請建立可撤銷、具最小權限與到期日的
專用 project access token 或 deploy token。

目前此專案的 `identity_refs` 只驗證 GitLab 身分的名稱；`config/secrets.yaml`
則供 Discord Secret materialization 使用，並不會把 GitLab PAT 注入 Agent。

若未來確實要在 Pod 中使用 GitLab token，至少要符合：

- K3s 已啟用 Secret at-rest encryption。
- 使用獨立 namespace 與專用 ServiceAccount。
- 僅將 Secret mount 或注入需要 Git 的單一容器。
- 不授予 Agent 對 Secret 的 `list`／`watch` 權限，也不授予不必要的 Pod 建立權限。
- 設定 token 到期、輪替與外洩後撤銷程序。

Kubernetes Secret 預設不保證在 etcd 加密，且能讀取 Secret、列出 Secret，或建立可掛載
Secret 的 Pod 的主體，都可能取得 Secret 值；Base64 編碼也不是加密。
請依 [Kubernetes Secret 安全建議](https://kubernetes.io/docs/concepts/security/secrets-good-practices/)
設定 at-rest encryption 與最小 RBAC。

## 禁止的做法

- 不要使用 `git config --global credential.helper store`：它會將 PAT 明文寫到磁碟。
- 不要用 `https://<PAT>@gitlab.com/...` 作為 remote URL；它容易被 shell history、`.git/config` 或 log 洩漏。
- 不要把 PAT 置於 `config/secrets.yaml`。該檔在此專案是供 Kubernetes Discord Secret materialization 使用，GitLab identity 只保留 reference。
- 不要將 PAT 貼到 issue、聊天、測試輸出或 commit。

## 驗證與撤銷

確認目前 remote 沒有 token：

```bash
git remote -v
```

確認 helper 設定：

```bash
git config --global --get-all credential.helper
```

若 PAT 遺失、外洩、員工角色變動或不再使用，立刻在 GitLab 的 Access tokens 頁面 revoke，
再建立新的最小權限 PAT 並重新登入。
