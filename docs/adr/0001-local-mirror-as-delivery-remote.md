# 0001. Delivery remote 使用本機 mirror，而非上游 GitLab

日期：2026-09-04
狀態：已採用

## 背景

`worktree-materialize` 會把 agent checkout 的 `origin` 設為 delivery remote，
並對它執行 `fetch`。因此 delivery remote 必須是可連線的。

授權的 repository 來自公司內部 GitLab。原始設計假定 delivery remote
就是那個上游位址。實際嘗試後發現兩個問題：

1. 控制主機（WSL）沒有該 GitLab 的 SSH 金鑰。金鑰在 Windows 側，
   而 drvfs 掛載的檔案權限是 0777，`ssh` 會拒絕使用。
   要能連線就必須把私鑰複製進 WSL。
2. 更根本的是：一旦 origin 指向真實 GitLab，**每個 agent 的 checkout
   都具備往正式 repository push 的管道**。而 agent 的行為只能靠勸告約束。

## 決策

Delivery remote 指向 `~/git-remotes/` 底下的本機 bare mirror
（`file://` URL），由 `scripts/mirror-sources.sh` 從 source repository 建立。

Mirror 的 `refs/heads/*` 呈現**上游有什麼**，而非主機上某個開發者
checkout 過什麼——`git clone --mirror` 從非 bare 來源複製時只會帶走
本機分支，因此 clone 後會把 `refs/remotes/origin/*` 提升為 heads。

## 後果

**得到**

- 不需要複製任何憑證進 WSL。
- Agent 完全無法接觸上游 GitLab，即使被注入指令也一樣。
- 不需要內網連線即可運作。

**付出**

- 上游有更新時必須手動重跑 `scripts/mirror-sources.sh` 同步。
- Mirror 佔用額外磁碟空間。
- 交付路徑因此不能依賴 push，必須改用主機端收取
  （見 [規格 §5](../規格-agent-工作流程.md)）。

## 替代方案

**指向真實 GitLab** — 被否決。需要複製私鑰，且授予 agent push 能力後，
只能靠勸告限制它怎麼用。這與「邊界應由機制而非請求實現」相衝突。

**唯讀 GitLab 憑證** — 部分可行，但仍需複製憑證，
且無法阻止 agent 讀取未授權的其他專案。
