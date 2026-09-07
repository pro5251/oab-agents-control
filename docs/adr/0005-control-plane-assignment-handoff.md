# 0005. 派工與審查交接由控制平面以 assignment 執行

日期：2026-09-07
狀態：已採用

Leader agent 沒有任意跨 Discord 頻道送訊息的能力，而 code task 又固定綁定 developer；
讓 leader agent 自己「找 reviewer」會退化成本機 subagent，也無法讓獨立 reviewer clone
看見 developer 尚未交付的 commit。決策是保留 task 的交付 owner，於 task 底下建立不可變
work assignment；控制平面以 leader bot 派工，並把精確 review subject、Discord provenance
與 reviewer 結果綁成 Review Record。

Code review subject 透過本機 mirror 的 `refs/oab/reviews/<task>/<sha>` 交接，reviewer
detached checkout 精確 SHA；完整指示放在 repository 外的唯讀 agent inbox。這刻意不採
「改派原 task」、「另建 reviewer task」、「先 push 上游」或「讓 leader agent 取得派工
MCP」：它們分別會破壞交付 ownership、產生 review-of-review／佔用 task slot、把內部驗證
綁到外部交付，或把可被 repository prompt injection 操縱的跨 agent 能力交給模型。

Leader runtime 同時必須以 ConfigMap 唯讀掛載受管 Codex config，停用 local multi-agent
capability；即使日後能力因版本漂移而誤開，缺少真正 reviewer bot、Discord message/thread
與相同 subject digest 的輸出也不得滿足驗收閘門。這使「沒有假 reviewer」同時具有
capability 邊界與 evidence 邊界。
