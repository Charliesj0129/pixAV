# 開發入口

先遵循 [AGENTS.md](AGENTS.md)；目標架構、目前實作、操作守衛與完整 BDD 見 [README.md](README.md)。完成標準仍是刪除 staging 與清空播放 cache 後，可從 Google Photos 恢復整片播放與 seek。

2026-10-12 設計 checkpoint 退休舊 application Compose 與啟動入口。[Windows Android runner](docker-compose.android-runner.yml) 及 [CI PostgreSQL／Redis](docker-compose.integration.yml) 保留；新的 [managed Compose](docker-compose.managed.yml) 接入 `windows-tools`、僅含 PostgreSQL／Redis 的 `core`、schema／authority、storage／playback、vpn／torrent／media、`resolver` 與 `library` profiles。使用 Microsoft WSL kernel，不恢復自訂 redroid kernel。完整應用部署、Windows managed ownership、私有播放與可回復部署仍待完整驗收。

操作者授權丟棄本輪舊影片、分段、回載、播放 cache 與影音錄影，不備份影片本體。DB／roles／ledger／queue、guest／owner、認證、未知上傳 operation 與 JSON audit 保留在 D 槽。舊 cohort 的 local path、publication 與歷史收據不代表媒體仍存在，也不抵新一輪驗收；禁止自動 resume／DLQ replay／盲目重傳。

下一個 agent 從 README 的「設計 checkpoint 與重跑起點」開始，以新 cohort 跑完整 Golden Path。軟體檢查、Photos cold integrity、人工影音／seek、二十四小時 quota、backup／restore、rollback 與 production gate 分開驗收，未取得當次證據保持 OPEN。

Linux 媒體共同鎖、固定 parent descriptor unlink 與 FFmpeg 子程序繼承鎖已有合成隔離證據；025 expand migration 另要求當次 inode／DB／資產／publication binding，舊授權不回填。026 的登錄交易、intent／audit 摘要及合成 restore 已通過本輪專用 PostgreSQL；私有 `manage_cleanup files / authorize` 的實際子程序已在專用 Docker／PostgreSQL／Redis 通過身分／owned pause、dry-run 零 regular-file metadata／DB 變更與可重入登錄。原 DB 未套用，入口不刪媒體、不新增備份。全部 reader、qBittorrent quiescence、整組授權及完整 cleanup apply／recover 操作仍待完成，正式 cleanup 未開啟。操作者確認刻意停止或重啟後，原 core 維持停止；恢復前重新核對掛載與 instance／pause ownership。

local stream、Photos 完整解碼與 read-back copy 亦納入 Linux 鎖協定；既存或被替換的 playback work 保留待覆核，不能由錯誤清理刪除或當成遠端完整性失敗。

segment staging 的 prepared／copy 輸出與 release 亦使用共同鎖；原件缺失、reader、異內容、foreign target、pending 或取消均保留相應副本。真跨 tmpfs 與取消只有本機合成證據，ADB push 全程、native Windows 與完整 reader 覆蓋仍待驗收。

canary ZIP、original copy、驗收 hash 與 HTTP movie acceptance 已接共同鎖；partial 被替換或發布衝突時保留 work，不能判為遠端完整性失敗。real A/V reference 與 recording／JSON 的整輪 session 鎖、FFmpeg descriptor 繼承及 0600 clip 已通過合成多窗口／父程序 SIGKILL 契約。synthetic recording 的每個窗口也持錄影鎖，capture 重算另持全部 JSON 鎖至結束；五個單窗口加五個重算窗口的四十次真 FFmpeg 解碼與父程序 SIGKILL 契約通過；中途換檔、證據改寫或解碼診斷拒絕。canary probe／decode、合成原件產生、ADB 與其他 reader／writer 覆蓋仍待完成，不能以單次 hash 或隔離證據開啟 cleanup。

取消會等待已開始的檔案 thread 與 cold child 收斂，重複取消不能跳過；child 移除未確認或 cache rename 已越界時保留 work 供 reconciliation，不宣告 domain 成功。未確認 child 另存私有 sibling 記錄，取消仍向外傳遞；pending 記錄阻擋同一 work 重建 child。Docker Desktop 已恢復同一 daemon，CI source bind 已重核；程式 checkpoint `4ac328c` 的完整檢查為 2,375 passed／8 skipped、coverage 85%，對應不可變映像已核對 source，實際斷網 child 取消／保全及 FFmpeg 檔案鎖契約通過。上述隔離證據不抵 Photos／完整 cleanup PASS。

原 checkpoint named-volume DB 的 offline identifier、三十張表內容摘要、二十一個 roles 屬性與 migration ledger 已唯讀重核；Redis 持久鍵相符，原 TTL 已過的鍵只在 RAM 查詢副本中到期，來源指紋不變。保留 guest bind 的實際 device／inode 與五千三百八十個檔案 size／SHA-256 相符。新 managed bind DB 是另一個 cluster／database，不能把其隔離空表當成原資料遷移；Mountpoint 字串的 bind alias 也不能代替 named-volume 身分。原 managed 服務保持停止，未新增備份、未啟動 guest；四個 managed 服務的十七個檔案／目錄 bind 與 Docker socket 已核對實際身分及內容／daemon，原設定／資料清單的五千一百八十四個檔案相符，另有一個不在舊備份基線的私有 secret 保留。原 Jellyfin SQLite 工作副本完整性與觀看狀態可讀，來源指紋未變，未做舊觀看紀錄逐列 digest 比對。live instance／pause ownership、未知副作用 reconciliation 與完整 Stage 0 仍待驗收。

備份及還原不得放 C 槽。Ubuntu 的 C 槽 LocalState 是指向 D 槽的 junction；Docker 資料磁碟已移至 `D:\DockerDesktopData`，Android SDK 已移至 `D:\Android\Sdk`，原 C 槽資料副本已清除。每次仍依當前檔案及成功收據核實，不能沿用舊 registration 或搬移腳本。私有 cleanup manifest／備份收據不進 Git。
