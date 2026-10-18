# 開發入口

先遵循 [AGENTS.md](AGENTS.md)；目標架構、目前實作、操作守衛與完整 BDD 見 [README.md](README.md)。完成標準仍是刪除 staging 與清空播放 cache 後，可從 Google Photos 恢復整片播放與 seek。

2026-10-12 設計 checkpoint 退休舊 application Compose 與啟動入口。[Windows Android runner](docker-compose.android-runner.yml) 及 [CI PostgreSQL／Redis](docker-compose.integration.yml) 保留；新的 [managed Compose](docker-compose.managed.yml) 接入 `windows-tools`、僅含 PostgreSQL／Redis 的 `core`、schema／authority、storage／playback、`vpn`、`resolver` 與 `library` profiles。使用 Microsoft WSL kernel，不恢復自訂 redroid kernel。完整應用部署、Windows managed ownership、私有播放與可回復部署仍待完整驗收。

操作者授權丟棄本輪舊影片、分段、回載、播放 cache 與影音錄影，不備份影片本體。DB／roles／ledger／queue、guest／owner、認證、未知上傳 operation 與 JSON audit 保留在 D 槽。舊 cohort 的 local path、publication 與歷史收據不代表媒體仍存在，也不抵新一輪驗收；禁止自動 resume／DLQ replay／盲目重傳。

下一個 agent 從 README 的「設計 checkpoint 與重跑起點」開始，以新 cohort 跑完整 Golden Path。軟體檢查、Photos cold integrity、人工影音／seek、二十四小時 quota、backup／restore、rollback 與 production gate 分開驗收，未取得當次證據保持 OPEN。

備份及還原不得放 C 槽。Ubuntu 的 C 槽 LocalState 是指向 D 槽的 junction；Docker 資料磁碟已移至 `D:\DockerDesktopData`，Android SDK 已移至 `D:\Android\Sdk`，原 C 槽資料副本已清除。每次仍依當前檔案及成功收據核實，不能沿用舊 registration 或搬移腳本。私有 cleanup manifest／備份收據不進 Git。
