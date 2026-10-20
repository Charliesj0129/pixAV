# 開發入口

先遵循 [AGENTS.md](AGENTS.md)；目標架構、目前實作、操作守衛與完整 BDD 見 [README.md](README.md)。完成標準仍是刪除 staging 與清空播放 cache 後，可從 Google Photos 恢復整片播放與 seek。

2026-10-12 設計 checkpoint 退休舊 application Compose 與啟動入口。[Windows Android runner](docker-compose.android-runner.yml) 及 [CI PostgreSQL／Redis](docker-compose.integration.yml) 保留；新的 [managed Compose](docker-compose.managed.yml) 接入 `windows-tools`、僅含 PostgreSQL／Redis 的 `core`、schema／authority、storage／playback、vpn／torrent／media、`resolver` 與 `library` profiles。使用 Microsoft WSL kernel，不恢復自訂 redroid kernel。完整應用部署、Windows managed ownership、私有播放與可回復部署仍待完整驗收。

操作者授權丟棄本輪舊影片、分段、回載、播放 cache 與影音錄影，不備份影片本體。DB／roles／ledger／queue、guest／owner、認證、未知上傳 operation 與 JSON audit 保留在 D 槽。舊 cohort 的 local path、publication 與歷史收據不代表媒體仍存在，也不抵新一輪驗收；禁止自動 resume／DLQ replay／盲目重傳。

下一個 agent 從 README 的「設計 checkpoint 與重跑起點」開始，以新 cohort 跑完整 Golden Path。軟體檢查、Photos cold integrity、人工影音／seek、二十四小時 quota、backup／restore、rollback 與 production gate 分開驗收，未取得當次證據保持 OPEN。

Linux 媒體共同鎖、固定 parent descriptor unlink 與 FFmpeg 子程序繼承鎖已有合成隔離證據；025 expand migration 另要求當次 inode／DB／資產／publication binding，舊授權不回填。026 收據核准、私有 `manage_cleanup files / authorize` 入口及 intent／audit 授權摘要已有本機實作，尚未跑新版 PostgreSQL／restore；入口不刪媒體、不新增備份。全部 reader、qBittorrent quiescence、整組授權及完整 cleanup apply／recover 操作仍待完成，正式 cleanup 未開啟。操作者確認刻意停止或重啟後，原 core 維持停止；恢復前重新核對掛載與 instance／pause ownership。

local stream、Photos 完整解碼與 read-back copy 亦納入 Linux 鎖協定；既存或被替換的 playback work 保留待覆核，不能由錯誤清理刪除或當成遠端完整性失敗。

segment staging 的 prepared／copy 輸出與 release 亦使用共同鎖；原件缺失、reader、異內容、foreign target、pending 或取消均保留相應副本。真跨 tmpfs 與取消只有本機合成證據，ADB push 全程、native Windows 與完整 reader 覆蓋仍待驗收。

取消會等待已開始的檔案 thread 與 cold child 收斂，重複取消不能跳過；child 移除未確認或 cache rename 已越界時保留 work 供 reconciliation，不宣告 domain 成功。未確認 child 另存私有 sibling 記錄，取消仍向外傳遞；pending 記錄阻擋同一 work 重建 child。目前 Docker Desktop 關閉，本版完整整合與新映像未重驗；先前斷網 child 證據不升級為本版或 Photos／完整 cleanup PASS。

備份及還原不得放 C 槽。Ubuntu 的 C 槽 LocalState 是指向 D 槽的 junction；Docker 資料磁碟已移至 `D:\DockerDesktopData`，Android SDK 已移至 `D:\Android\Sdk`，原 C 槽資料副本已清除。每次仍依當前檔案及成功收據核實，不能沿用舊 registration 或搬移腳本。私有 cleanup manifest／備份收據不進 Git。
