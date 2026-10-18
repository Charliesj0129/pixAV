# 開發規則

pixAV 的目標是將外部媒體轉成已驗證、可長期由 Google Photos 恢復播放的私人資產。專案用途、目標架構、現況差距、開發順序與完整 BDD 集中於 [README.md](README.md)；[CLAUDE.md](CLAUDE.md) 僅作入口。開發以 README 的 Golden Path 與 BDD ID 對應驗收，不能把計畫、測試存在或舊實驗寫成當前部署事實。

## 工作方式

- 開始前讀相關 `.agent/skills/`、`.agent/workflows/` 與 `user_rules.md`；不存在時記錄並繼續，不虛構 workflow。
- 核對 worktree，先讀實際 repository、callers、測試、migration、Compose 與可核實 runtime evidence；保留既有程式修改。區分目標契約、目前實作、隔離證據與 production 驗收。
- 舊 application Compose／start_workers 與 first-4k 啟動入口已退休；獨立 Android runner 與 CI integration Compose 保留。新 managed Compose 依已驗收 profile 分段接入，目前有 `windows-tools`、僅含 PostgreSQL／Redis 的 `core` 及 schema／authority profiles；完整應用部署／Windows upload adapter 另行驗收，不復活舊 executor 或重播舊 queue。
- Windows 開發環境採 Windows Android Emulator + Docker Runner，WSL／Docker Desktop 使用 Microsoft 提供的 WSL kernel。操作者已回報 `bzImage-6.6.87.2-pixav-redroid` 的 `.wslconfig` kernel override 造成相容性問題；不得把修改全域 kernel、載入 binder 或 privileged redroid guest 作為開發前置。設定範圍、ADB／Maestro 與遷移守衛見 README。
- 維持 README、AGENTS、CLAUDE 三份核心文件，不新增 roadmap、CODEMAPS、ADR、實驗流水帳、archive 或轉址頁。外來規格整合進 README 並保留情境對應；文件刪改直接進行，不建立文件備份，同步修正引用。
- 文件採中性用語與合成媒體，不放來源名稱、連結、作品識別資料或相關圖片；實際程式路徑、參數、設定鍵保留原名。私有 evidence 不變成公開文件或 fixture。

## 核心架構

- 自行維護的業務邏輯限於 SourcePolicy、MediaWorkflow、GooglePhotosStorage、PlaybackResolver、LibraryProjection。交給成熟 OSS 執行 torrent、codec／remux、播放器、library UI 與通用基礎設施；不因邏輯邊界就要求立即拆服務或更名目錄。
- PostgreSQL 是 domain SSOT；orchestration 選定後只能有一套 execution SSOT，只有它可推進 execution、安排 retry／timer。domain task 與 attempt 分離，切換時禁止新舊 executor 同時寫同一執行狀態。
- 保留來源 normalization/scoring、hard eligibility、provenance、候選切換與 cooldown。來源失效不污染 Video 身分；零 seeds 不單獨判永久失效，基礎設施錯誤不污染候選。不假設不同 info hash 已完成媒體去重。
- 保留帳號 LRU/lease/quota/cooldown；upload concurrency 固定 1。指定 Pixel-compatible upload environment 不得被靜默替換；Photos 隔離驗證，rclone fallback 必須明確計入配額。
- RemoteAsset、PlayableAsset、LibraryPublication 與 Execution 各自表達狀態；既有 COMPLETE、AVAILABLE 或 share URL 不可直接升級為 DURABLE。Models immutable，外部 payload 在 adapter 邊界驗證，metadata 按 provider namespace 合併並保留 provenance，manual override 優先且 refresh 不覆蓋。
- 未知上傳結果的覆核使用 `review-upload-outcome` dry-run；apply 只登錄請求，由 Maxwell 驗證原 operation／generation／evidence 後推進。缺分享位置的 v2 candidate 覆核只允許核對既有 exact item、journal 分享及全新 cold 回載，不能 provision／替換 guest、push 或 publish；分享不等於成功。搜尋未命中不能授權重傳；補充影音窗口須完整重錄，人工實看實聽前保持 REVIEW_REQUIRED。
- 外部副作用先記可 reconciliation 的 intent／operation identity；crash 後先確認結果再重做。遠端成功與 quota 以可重入交易避免重複提交；結果不明時等待 reconciliation 或人工處理，不能盲目重傳。
- Google Photos 上傳 UI 或分享頁 HTTP 成功不等於 durable。必須新 session、cold path、無原始 staging／上傳片段可冒充回載，通過版本化的 size、duration、codec、hash 等完整性政策後才能 DURABLE。
- Jellyfin 只使用 video_id 的 pixAV 穩定 URL；短命 provider URL 不寫 projection、不向 Jellyfin 暴露。保留 Photos 專屬 resolver、cold reload、Range／seek、分段及 cache 政策，通用傳輸能力委派經驗收的 OSS。
- 只有 PlayableAsset READY 才原子發布 `.strm + .nfo + artwork`；可重建、可 reconciliation、可撤架，撤架不刪 remote asset。展示、搜尋、收藏與觀看紀錄交 Jellyfin，不另建 catalog/search framework。
- 完整 Phase 0 通過後才逐段退休 fallback。不擴寫通用 crawler、queue/DLQ/retry、UI automation 或 media proxy framework；不預選 orchestrator，不同時維護 Stash/MetaTube，不安排 Rust rewrite。

## 操作安全

- 僅啟動當前階段必要的容器，優先重用已核對身分的既有服務；不得整組啟動未需要的服務或另建重複部署。臨時驗證容器逐一執行、完成即移除；錯誤或已被取代的部署以 exact container ID 移除，保留資料 volumes、staging 與證據，不使用 prune。
- Android 開發 runner 使用獨立 Compose／profile、Windows ADB server 與 exact serial，不掛 Docker socket、AVD、帳號 secret 或 DB／queue credential，也不啟動 executor。ADB port 不對 LAN／公網開放；Maestro forwarding 須另驗。既有 redroid managed runtime 尚待遷移，不以開發入口或舊 profile 證明 Pixel-compatible／quota 契約已通過；保留原 guest、intent 與未知上傳結果，先 reconciliation 再切換。
- 資料操作、備份、cleanup、migration、replay 前核對 Docker daemon/context、DB identifier/database、Redis run ID；localhost、container name 與歷史值不是 identity 證明。 主機／daemon 恢復後亦核對必要 bind mount 的 host／container 實際目錄、device／inode 與內容；inspect 的 Source 字串相同不足以證明掛載正確，空掛載不可誤當資料遺失而覆寫。
- 破壞性資料 script 預設 dry-run；dry-run 不改資料、queue 或檔案。apply 要同實例 full backup/sidecar、exact target/count、selected-row/queue backup。備份從建立起 0600、目錄 0700，離機加密保存並驗證 restore；此規則適用資料，不要求文件備份。
- 本次階段 3–5 隔離驗收依操作者授權，允許本機加密完整備份加同機獨立完整還原，作為本輪 cleanup 的備份前置例外；仍須通過一致快照、roles／ledger／queue／guest／觀看紀錄及精確刪除守衛。例外不代表離機備份、整機故障恢復或 production promotion 通過。
- 2026-10-15 操作者指示本輪略過離機備份規則，停止新增備份；保留已建立的本機備份與獨立還原資料。後續沿用此授權，不再次要求離機目的地；其他資料操作與驗收守衛仍適用。
- `.env`、`secrets/`、`backups/` 不進版控；credential 不進 log、model repr、metrics、fixture、projection 或 Maestro YAML。private playback 必須驗收未授權 client 與撤銷裝置被拒絕。
- 新 cohort 的真實影音採版本化 evidence，完整 manifest／reference／錄影與全部交界須可重算；自動 capture 不能代替操作者實看實聽，舊版人工證據不自動升級。
- 2026-10-12 操作者授權設計 checkpoint 重置：舊隔離影片／分段／回載／播放 cache／影音錄影可依 exact-target manifest 人工丟棄且不備份影片本體。先保留並驗證 D 槽的程式／設定／DB／roles／ledger／queue／guest／未知上傳紀錄與認證；此為一次性人工重置，不記 Golden Path PASS，不放寬正式 cleanup 或未知副作用 reconciliation。舊 cohort 的已刪媒體與錄影不抵新驗收，下一輪用新 cohort 重跑全部 gate。
- staging cleanup 必須同時滿足遠端 DURABLE、有效完整性證據、播放 READY 且實播驗證完成、完整 publication、retention 到期、無 active execution/reader、path/ownership 與操作守衛通過。資格檢查到 unlink 之間防止競態；cleanup crash 後核對 filesystem 並留下 audit。
- 磁碟餘額須同時核對 WSL／Docker VHD 所在實體分區；虛擬檔案系統餘額不代替實體容量。Docker 資料磁碟搬移須先完整盤點、獨立備份還原與容量守衛，再取得明確搬移授權；停止的其他專案不得隨 daemon 恢復而自動啟動。
- 本輪已授權的 Docker 搬移及精確舊 C 副本清除已完成，資料與開機 distro 位於 D 槽，未重啟全 WSL；後續新搬移或新增其他專案中斷範圍須另行核對授權。可重建 build／工具 cache 的清理須逐筆核對 manifest，保留映像、設定與重建入口；不可藉精簡清除媒體、guest、intents 或驗收 evidence，未核實 VHD 實體餘額前不宣稱已騰出 Windows 空間。
- 保留 local retention、100 GiB／10% disk latch、symlink/path containment、open-task guard、FIFO/in-flight recovery、有限 retry。磁碟壓力或 terminal failure 不豁免 durability gate。DB clock 決定 quota/retry 與持久期限；heartbeat 用 monotonic clock。
- 2026-10-18 操作者授權本輪隔離續作至 D 槽剩餘 25 GiB 才停止；本輪私有容量設定可覆寫 100 GiB／10% 預設，在實體餘額小於等於 25 GiB 時停止新增工作並收斂既有操作。預設政策及其餘資料／durability 守衛不變。
- `pixav-local://` 播放檔不可當遠端備份而刪除；projection 不完整拒絕 cleanup。cache 可重建但 eviction 仍等待 reader 釋放；首段不能代表整片，缺段與 manifest 不符拒絕 READY。
- 只用自己的 token 解除 pause，不直接刪 key；DLQ 不自動 replay，人工 replay 留操作者、原因與原 attempt 歷史。VPN kill switch 阻流且須實測，出口 IP 比對只告警。
- 登入 challenge 停 retry 進 USER_ACTION_REQUIRED 交人工，不重送帳密。保留 ownership、guest/checkpoint 與未知副作用的 reconciliation 資料。
- Android 帳號以 `Accounts:` 與實際 Account rows 核對，不把 authenticator 註冊算成已登入帳號。人工 scrcpy bootstrap 期間維持 system pause 並停止 native managed control；交回前核對及關閉 exact controller／Android child，重驗 quiescence 與當次 enrollment，避免並行 UI writer。
- 原帳號 cold 模式只接受私有 cookie-only secret reference，經 stdin 傳入新 context；child 僅掛空白工作目錄。登入收據須核對帳號／owner／item／session／原件 digest 與完整解碼，不以匿名歷史收據補齊新契約；UI 鎖失聯先停止 exact child，不能留下仍操作帳號的背景下載。
- 新 adapter/服務在 README 記錄 timeout、health/metrics、secret 管理、版本/license、volume、資源限制、備份與 rollback；外部契約須實測。migration 先驗 expand/contract 與 restore，不讓不相容的舊 process 繼續執行。

## 驗證

```bash
uv run pytest -q --cov=src/pixav --cov-report=term-missing --cov-report=xml
uv run ruff check src tests scripts
uv run black --check src tests scripts
uv run isort --check-only src tests scripts
uv run mypy src
docker compose -f docker-compose.integration.yml config -q
# Set PIXAV_ANDROID_RUNNER_IMAGE, PIXAV_ANDROID_SERIAL and PIXAV_ANDROID_RUNNER_STAGING_DIR first.
docker compose -f docker-compose.android-runner.yml --profile android-dev config -q
```

外部整合優先 live contract、真實 fixture、真 PostgreSQL repository/migration 與 opt-in E2E；mock 依已驗證契約。BDD 情境 ID 對應測試與私有 evidence；尚未引入的 pytest-bdd／marker／測試目錄不可寫成已可執行功能。CI 必須啟用專用 PostgreSQL／Redis 核心整合測試並核對 instance identity；coverage 保留完整報告，不設全域百分比門檻。Coverage 不代表 production 可用，既有失敗另行記錄，不接管未核對的正式資料或服務。

Golden Path 必須在真實環境證明：Photos cold read-back 完整性通過 → DURABLE → READY → Jellyfin projection／影音播放／seek → 安全刪 staging → 清播放 cache → 再由 Photos 恢復播放／seek，而且無重複上傳或 quota。local finalize、單段播放或 HTTP HEAD 不足以通過。

每段 promotion 要 contract、隔離 integration、live、故障恢復、rollback 與唯一 execution ownership 通過；critical 情境失敗或證據缺失即阻擋。未實播、未滿 24 小時 quota 觀察或未核實項目維持 OPEN／未驗收。

純文件修改檢查連結、命令語法、BDD 完整性與差異範圍；程式未變且上一輪完整檢查已通過時，不重跑同一套測試。未執行的檢查不得宣稱通過。
