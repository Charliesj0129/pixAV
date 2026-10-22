# 開發入口

先遵循 [AGENTS.md](AGENTS.md)；目標架構、目前實作、操作守衛與完整 BDD 見 [README.md](README.md)。完成標準仍是刪除 staging 與清空播放 cache 後，可從 Google Photos 恢復整片播放與 seek。

2026-10-12 設計 checkpoint 退休舊 application Compose 與啟動入口。[Windows Android runner](docker-compose.android-runner.yml) 及 [CI PostgreSQL／Redis](docker-compose.integration.yml) 保留；新的 [managed Compose](docker-compose.managed.yml) 接入 `windows-tools`、僅含 PostgreSQL／Redis 的 `core`、schema／authority、storage／playback、vpn／torrent／media、`resolver` 與 `library` profiles。使用 Microsoft WSL kernel，不恢復自訂 redroid kernel。完整應用部署、Windows managed ownership、私有播放與可回復部署仍待完整驗收。

操作者授權丟棄本輪舊影片、分段、回載、播放 cache 與影音錄影，不備份影片本體。DB／roles／ledger／queue、guest／owner、認證、未知上傳 operation 與 JSON audit 保留在 D 槽。舊 cohort 的 local path、publication 與歷史收據不代表媒體仍存在，也不抵新一輪驗收；禁止自動 resume／DLQ replay／盲目重傳。

下一個 agent 從 README 的「設計 checkpoint 與重跑起點」開始，以新 cohort 跑完整 Golden Path。軟體檢查、Photos cold integrity、人工影音／seek、二十四小時 quota、backup／restore、rollback 與 production gate 分開驗收，未取得當次證據保持 OPEN。

Linux 媒體共同鎖、固定 parent descriptor unlink 與 FFmpeg 子程序繼承鎖已有合成隔離證據；025 expand migration 要求當次 inode／DB／資產／publication binding，舊授權不回填。026 登錄交易及合成 restore 已通過專用 PostgreSQL；私有 `manage_cleanup files / authorize` 實際子程序已通過 runtime／owned pause、dry-run 零 regular-file metadata／DB 變更與可重入登錄。新的 `execute / recover` 綁定既有核准全集與 exact count，保留 eligible 鄰檔，防止 execution／asset 競態，中途拒絕即停止，unlink 後 SIGKILL 可精確收斂而不重刪；專用 Docker／PostgreSQL／Redis 的實際 CLI 已通過。`capture-downloader` 只觀測 mount，真 qBittorrent v5.2.3 的斷網合成測試已核對實際 inode、停止狀態與重啟 epoch 拒絕。dry-run 已補目標樹全部檔案／目錄／symlink metadata 與跨影片路徑寫入守衛，projection 只在唯讀／noatime 或程序私有唯讀 namespace 驗證；configured root alias、跨影片 hardlink 與不明引用拒絕。全部 actor fencing、共用檔案整組授權及其他 filesystem 仍待完成，正式 cleanup 未開啟；原 DB 未套 migration、未 apply、不新增備份。原 core 維持停止，恢復前重核掛載與 instance／pause ownership。

local stream、Photos 完整解碼與 read-back copy 亦納入 Linux 鎖協定；既存或被替換的 playback work 保留待覆核，不能由錯誤清理刪除或當成遠端完整性失敗。

segment staging 的 prepared／copy 輸出與 release 亦使用共同鎖；原件缺失、reader、異內容、foreign target、pending 或取消均保留相應副本。真跨 tmpfs 與取消只有本機合成證據，ADB push 全程、native Windows 與完整 reader 覆蓋仍待驗收。

canary ZIP、original copy、驗收 hash 與 HTTP movie acceptance 已接共同鎖；partial 被替換或發布衝突時保留 work，不能判為遠端完整性失敗。real A/V reference 與 recording／JSON 的整輪 session 鎖、FFmpeg descriptor 繼承及 0600 clip 已通過合成多窗口／父程序 SIGKILL 契約。synthetic recording 的五個單窗口加五個重算窗口、四十次真 FFmpeg 解碼與父程序 SIGKILL 契約通過；中途換檔、證據改寫或解碼診斷拒絕。canary collector 已補齊同一 descriptor 從 hash、probe、完整 decode 至 receipt，合成原件以 0600 exclusive create 及 FFmpeg inherited descriptor 持鎖；真 probe／decoder／encoder 父程序 SIGKILL 契約通過，精簡 helper 不依賴業務服務。本次正式 `download_original` 另補整輪原件鎖、快取收據鎖及原子 pending receipt；來源相符的實際 storage-tools 映像以 FFmpeg 6.1.1、二秒 320×180 斷網 fixture，通過真 observation／decode、換檔拒絕、子程序鎖延續與收據 SIGKILL 保全。browser 是合成輸入，沒有 Photos 或真實帳號請求。正常工具讀取可能更新 atime，cleanup dry-run 仍須零 metadata 變更。舊 Docker source probe、ADB、native Windows 與其他 reader／writer 覆蓋仍待完成，不能開啟 cleanup。

取消會等待已開始的檔案 thread 與 cold child 收斂，重複取消不能跳過；child 移除未確認或 cache rename 已越界時保留 work 供 reconciliation，不宣告 domain 成功。未確認 child 另存私有 sibling 記錄，取消仍向外傳遞；pending 記錄阻擋同一 work 重建 child。Docker Desktop 已恢復同一 daemon，十七個 managed bind 與 socket 已有重核證據；先前 checkpoint 的 child 取消／保全、唯讀 projection 與 canary／generator 契約各保留原來源 scope。目前程式 checkpoint `e642b25` 完整檢查為 2,436 passed／8 skipped、coverage 84%，Ruff／Black／isort／mypy 通過，未變的 Compose 沿用先前設定檢查；本次 storage-tools 映像的一百五十個 source 檔案及整輪 cold session／原子收據契約已核對，尚未部署到原服務。上述隔離證據不抵 Photos／完整 cleanup PASS。

操作者已回覆登入，當次 AVD 核對到一個實際 Google account row；Windows scrcpy 與 Android scrcpy 子程序皆已不存在，人工 owner 記錄已收斂。設定帳號／owner binding、當次完整環境及 enrollment 仍為 OPEN；native control 維持 `HALTED`，帳號自動操作與上傳暫停。目前 4 核心／4096 MiB／host GPU 的實際 launch 與 verifier 固定的舊 2 核心／軟體 GPU 契約不符，下一步補正式版本契約並重新驗收，不能沿用舊 boot 證據。

原 checkpoint named-volume DB 的 offline identifier、三十張表內容摘要、二十一個 roles 屬性與 migration ledger 已唯讀重核；Redis 持久鍵相符，原 TTL 已過的鍵只在 RAM 查詢副本中到期，來源指紋不變。保留 guest bind 的實際 device／inode 與五千三百八十個檔案 size／SHA-256 相符。新 managed bind DB 是另一個 cluster／database，不能把其隔離空表當成原資料遷移；Mountpoint 字串的 bind alias 也不能代替 named-volume 身分。原 managed 服務保持停止，未新增備份、未啟動 guest；四個 managed 服務的十七個檔案／目錄 bind 與 Docker socket 已核對實際身分及內容／daemon，原設定／資料清單的五千一百八十四個檔案相符，另有一個不在舊備份基線的私有 secret 保留。原 Jellyfin SQLite 工作副本完整性與觀看狀態可讀，來源指紋未變，未做舊觀看紀錄逐列 digest 比對。live instance／pause ownership、未知副作用 reconciliation 與完整 Stage 0 仍待驗收。

備份及還原不得放 C 槽。Ubuntu 的 C 槽 LocalState 是指向 D 槽的 junction；Docker 資料磁碟已移至 `D:\DockerDesktopData`，Android SDK 已移至 `D:\Android\Sdk`，原 C 槽資料副本已清除。每次仍依當前檔案及成功收據核實，不能沿用舊 registration 或搬移腳本。私有 cleanup manifest／備份收據不進 Git。
