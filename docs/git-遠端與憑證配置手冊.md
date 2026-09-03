# Git 遠端與憑證配置手冊

本手冊說明如何從 WSL 將 `oab-agents-control` 上傳到 GitHub，以及專案內 GitLab
reference 的用途。Private key、PAT 與其他憑證不可提交、貼到聊天或存入 catalog、
`config/secrets.yaml`、K3s Secret。

## 選擇認證方式

| 用途／remote URL | 認證方式 | 建議 |
| --- | --- | --- |
| 上傳此專案至 `git@github.com:<owner>/<repo>.git` | SSH key | 建議；私鑰保留在 WSL |
| 使用 `https://github.com/...` 或 `https://gitlab.com/...` | PAT + Git Credential Manager（GCM） | 僅在必須使用 HTTPS 時採用 |
| OpenAB catalog 的 `gitlab_identity_ref` | 只有身分名稱 reference | 不是 token、SSH key 或登入設定 |

GitHub source remote 與 OpenAB runtime 的 GitLab `identity_refs` 是不同概念：將此
repository 上傳到 GitHub，不會自動改變 OpenAB 的 GitLab delivery metadata。

## GitHub SSH 設定（WSL）

### 1. 建立專用 key

先查看既有 key，避免覆寫：

```bash
ls -al ~/.ssh
```

若沒有 GitHub 專用 Ed25519 key，建立一把；設定安全的 passphrase：

```bash
ssh-keygen -t ed25519 -C "<你的-GitHub-email>" -f ~/.ssh/id_ed25519_github
```

設定 private key 只能由目前使用者讀取：

```bash
chmod 700 ~/.ssh
chmod 600 ~/.ssh/id_ed25519_github
chmod 644 ~/.ssh/id_ed25519_github.pub
```

### 2. 設定 GitHub 專用 SSH host

使用慣用編輯器建立或編輯 `~/.ssh/config`，加入：

```sshconfig
Host github.com
  HostName github.com
  User git
  IdentityFile ~/.ssh/id_ed25519_github
  IdentitiesOnly yes
  AddKeysToAgent yes
```

再限制設定檔權限：

```bash
chmod 600 ~/.ssh/config
```

`IdentityFile` 指定 GitHub 使用這把專用 key；`IdentitiesOnly yes` 可避免 SSH 嘗試
其他 key 而被 GitHub 拒絕。

### 3. 載入 ssh-agent

```bash
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/id_ed25519_github
```

這會要求輸入 SSH key 的 passphrase。private key 本身會持久化保存在 `~/.ssh`，但
agent 的解鎖快取通常在新 shell 或 WSL 重啟後需要重新執行上述兩行。

### 4. 加入 GitHub 帳號

複製 public key：

```bash
cat ~/.ssh/id_ed25519_github.pub
```

在 GitHub 開啟 **Settings → SSH and GPG keys → New SSH key**，選擇
**Authentication Key**，貼上 `.pub` 檔案內容。絕不可上傳
`~/.ssh/id_ed25519_github` private key。

### 5. 測試

```bash
ssh -T git@github.com
```

初次連線時先核對 GitHub host key fingerprint，再接受主機。

## 設定 Git 提交者身分

在首次 commit 前設定會寫入 commit metadata 的名稱與 email；這不是 SSH key，也不是
GitHub PAT：

```bash
git config --global user.name "<你的顯示名稱>"
git config --global user.email "<你的-GitHub-email>"
git config --global init.defaultBranch main
```

## 設定此專案的 GitHub remote 並推送

先在 GitHub 建立空的 `oab-agents-control` repository，不要初始化 README、
`.gitignore` 或 License。

```bash
cd ~/oab-agents-control
git remote add origin git@github.com:<帳號或組織>/oab-agents-control.git
git ls-remote origin
git status
```

確認 `git status` 不含 `config/secrets.yaml`、PAT、private key 或其他敏感檔案後：

```bash
git add .
git commit -m "Initial commit"
git branch -M main
git push -u origin main
```

若已有 `origin`，將 `git remote add` 改為：

```bash
git remote set-url origin git@github.com:<帳號或組織>/oab-agents-control.git
```

## 需要 HTTPS/PAT 時

HTTPS remote 才會使用 GCM 與 PAT；SSH remote 不會使用它們。不要將 PAT 放在 remote
URL 或使用 `credential.helper store`（會明文存檔）。在 Windows + WSL 上，GCM 可將
PAT 加密保存於 Windows Credential Manager。已安裝 Git for Windows 時，在 WSL 設定：

```bash
test -x "/mnt/c/Program Files/Git/mingw64/bin/git-credential-manager.exe"
git config --global --unset-all credential.helper
git config --global credential.helper '/mnt/c/Program\ Files/Git/mingw64/bin/git-credential-manager.exe'
```

若第一行失敗，請在 Windows 找到 `git-credential-manager.exe` 的實際位置並取代路徑。
完成後，使用 HTTPS remote 執行一次 `git ls-remote origin`，依提示輸入帳號與 PAT；
GCM 會保存它。使用本手冊建議的 SSH remote 時，不需要這段 GCM 設定。

若 OpenAB runtime 仍使用 GitLab，`identity_refs` 只保存如 `gitlab-bootstrap` 的名稱；
PAT／SSH key 本身必須由 Git credential helper 或既有的外部憑證系統管理。

## K3s 界線

不要將個人 SSH key 或 PAT 預設存入 K3s。只有 Pod 必須自行操作 Git 時，才建立專用、
最小權限、可輪替的 token，並啟用 Secret at-rest encryption 與最小 RBAC。

## `ssh -T` 失敗時的診斷

以下命令只顯示檔案名稱、權限、指紋與連線結果，不會顯示 private key 內容：

```bash
ls -l ~/.ssh/id_ed25519_github ~/.ssh/id_ed25519_github.pub ~/.ssh/config
ssh-add -l
ssh -G github.com | grep -E '^(user|hostname|identityfile|identitiesonly|addkeystoagent) '
ssh -T -o BatchMode=yes -o IdentitiesOnly=yes \
  -i ~/.ssh/id_ed25519_github git@github.com
```

如果 `ssh-add -l` 顯示沒有 identities，重新載入：

```bash
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/id_ed25519_github
```

如果指定 `-i` 後仍顯示 `Permission denied (publickey)`，請確認以下兩件事：

1. GitHub 帳號的 SSH key 設定中，已貼上**同一把** public key：
   `cat ~/.ssh/id_ed25519_github.pub`。
2. 本機指紋與 GitHub 設定中的指紋相符：

   ```bash
   ssh-keygen -lf ~/.ssh/id_ed25519_github.pub
   ```

常見錯誤判讀：

| 錯誤 | 意義與處理 |
| --- | --- |
| `Permission denied (publickey)` | 已連到 GitHub，但 key 未被該帳號接受；檢查 public key、key 檔案與 ssh-agent。 |
| `Could not resolve hostname github.com` | WSL DNS／網路問題，尚未進入 key 驗證。 |
| `Connection timed out` 或 `Network is unreachable` | 防火牆、代理或網路出口阻擋 SSH。 |
| `Host key verification failed` | `known_hosts` 中的主機指紋不符；不要盲目刪除，先核對 GitHub 官方 host key。 |
| `No such file or directory` | `-i` 或 `IdentityFile` 指向不存在的 key。 |

成功的 `ssh -T` 預期會回覆已驗證 GitHub 帳號、但 GitHub 不提供 shell access；這不是
錯誤。確認成功後，`git remote -v` 必須顯示 `git@github.com:...` 的 SSH URL，否則
Git 操作仍可能走 HTTPS／GCM。

## 官方文件

- [GitHub：產生 SSH key 並加入 ssh-agent](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/generating-a-new-ssh-key-and-adding-it-to-the-ssh-agent?platform=linux)
- [GitHub：將 SSH key 加入帳號](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/adding-a-new-ssh-key-to-your-github-account?platform=linux&tool=webui)
- [GitHub：測試 SSH 連線](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/testing-your-ssh-connection?platform=linux)
