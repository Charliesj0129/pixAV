# 開發規則

pixAV 的目標是將外部媒體轉成已驗證、可長期由 Google Photos 恢復播放的私人資產。專案用途、目標架構、現況差距、開發順序與完整 BDD 集中於 [README.md](README.md)；[CLAUDE.md](CLAUDE.md) 僅作入口。開發以 README 的 Golden Path 與 BDD ID 對應驗收，不能把計畫、測試存在或舊實驗寫成當前部署事實。

## 工作方式

- 開始前讀相關 `.agent/skills/`、`.agent/workflows/` 與 `user_rules.md`；不存在時記錄並繼續，不虛構 workflow。
- 核對 worktree，先讀實際 repository、callers、測試、migration、Compose 與可核實 runtime evidence；保留既有程式修改。區分目標契約、目前實作、隔離證據與 production 驗收。
- 維持 README、AGENTS、CLAUDE 三份核心文件，不新增 roadmap、CODEMAPS、ADR、實驗流水帳、archive 或轉址頁。外來規格整合進 README 並保留情境對應；文件刪改直接進行，不建立文件備份，同步修正引用。
- 文件採中性用語與合成媒體，不放來源名稱、連結、作品識別資料或相關圖片；實際程式路徑、參數、設定鍵保留原名。私有 evidence 不變成公開文件或 fixture。

## 核心架構

- 自行維護的業務邏輯限於 SourcePolicy、MediaWorkflow、GooglePhotosStorage、PlaybackResolver、LibraryProjection。交給成熟 OSS 執行 torrent、codec／remux、播放器、library UI 與通用基礎設施；不因邏輯邊界就要求立即拆服務或更名目錄。
- PostgreSQL 是 domain SSOT；orchestration 選定後只能有一套 execution SSOT，只有它可推進 execution、安排 retry／timer。domain task 與 attempt 分離，切換時禁止新舊 executor 同時寫同一執行狀態。
- 保留來源 normalization/scoring、hard eligibility、provenance、候選切換與 cooldown。來源失效不污染 Video 身分；零 seeds 不單獨判永久失效，基礎設施錯誤不污染候選。不假設不同 info hash 已完成媒體去重。
- 保留帳號 LRU/lease/quota/cooldown；upload concurrency 固定 1。指定 Pixel-compatible upload environment 不得被靜默替換；Photos 隔離驗證，rclone fallback 必須明確計入配額。
- RemoteAsset、PlayableAsset、LibraryPublication 與 Execution 各自表達狀態；既有 COMPLETE、AVAILABLE 或 share URL 不可直接升級為 DURABLE。Models immutable，外部 payload 在 adapter 邊界驗證，metadata 按 provider namespace 合併並保留 provenance，manual override 優先且 refresh 不覆蓋。
- 外部副作用先記可 reconciliation 的 intent／operation identity；crash 後先確認結果再重做。遠端成功與 quota 以可重入交易避免重複提交；結果不明時等待 reconciliation 或人工處理，不能盲目重傳。
- Google Photos 上傳 UI 或分享頁 HTTP 成功不等於 durable。必須新 session、cold path、無原始 staging／上傳片段可冒充回載，通過版本化的 size、duration、codec、hash 等完整性政策後才能 DURABLE。
- Jellyfin 只使用 video_id 的 pixAV 穩定 URL；短命 provider URL 不寫 projection、不向 Jellyfin 暴露。保留 Photos 專屬 resolver、cold reload、Range／seek、分段及 cache 政策，通用傳輸能力委派經驗收的 OSS。
- 只有 PlayableAsset READY 才原子發布 `.strm + .nfo + artwork`；可重建、可 reconciliation、可撤架，撤架不刪 remote asset。展示、搜尋、收藏與觀看紀錄交 Jellyfin，不另建 catalog/search framework。
- 完整 Phase 0 通過後才逐段退休 fallback。不擴寫通用 crawler、queue/DLQ/retry、UI automation 或 media proxy framework；不預選 orchestrator，不同時維護 Stash/MetaTube，不安排 Rust rewrite。

## 操作安全

- 資料操作、備份、cleanup、migration、replay 前核對 Docker daemon/context、DB identifier/database、Redis run ID；localhost、container name 與歷史值不是 identity 證明。
- 破壞性資料 script 預設 dry-run；dry-run 不改資料、queue 或檔案。apply 要同實例 full backup/sidecar、exact target/count、selected-row/queue backup。備份從建立起 0600、目錄 0700，離機加密保存並驗證 restore；此規則適用資料，不要求文件備份。
- `.env`、`secrets/`、`backups/` 不進版控；credential 不進 log、model repr、metrics、fixture、projection 或 Maestro YAML。private playback 必須驗收未授權 client 與撤銷裝置被拒絕。
- staging cleanup 必須同時滿足遠端 DURABLE、有效完整性證據、播放 READY 且實播驗證完成、完整 publication、retention 到期、無 active execution/reader、path/ownership 與操作守衛通過。資格檢查到 unlink 之間防止競態；cleanup crash 後核對 filesystem 並留下 audit。
- 保留 local retention、100 GiB／10% disk latch、symlink/path containment、open-task guard、FIFO/in-flight recovery、有限 retry。磁碟壓力或 terminal failure 不豁免 durability gate。DB clock 決定 quota/retry 與持久期限；heartbeat 用 monotonic clock。
- `pixav-local://` 播放檔不可當遠端備份而刪除；projection 不完整拒絕 cleanup。cache 可重建但 eviction 仍等待 reader 釋放；首段不能代表整片，缺段與 manifest 不符拒絕 READY。
- 只用自己的 token 解除 pause，不直接刪 key；DLQ 不自動 replay，人工 replay 留操作者、原因與原 attempt 歷史。VPN kill switch 阻流且須實測，出口 IP 比對只告警。
- 登入 challenge 停 retry 進 USER_ACTION_REQUIRED 交人工，不重送帳密。保留 ownership、guest/checkpoint 與未知副作用的 reconciliation 資料。
- 新 adapter/服務在 README 記錄 timeout、health/metrics、secret 管理、版本/license、volume、資源限制、備份與 rollback；外部契約須實測。migration 先驗 expand/contract 與 restore，不讓不相容的舊 process 繼續執行。

## 驗證

```bash
uv run pytest -q --cov=src/pixav --cov-fail-under=80
uv run ruff check src tests scripts
uv run black --check src tests scripts
uv run isort --check-only src tests scripts
uv run mypy src
docker compose config -q
docker compose -f docker-compose.yml -f docker-compose.prod.yml config -q
```

外部整合優先 live contract、真實 fixture、真 PostgreSQL repository/migration 與 opt-in E2E；mock 依已驗證契約。BDD 情境 ID 對應測試與私有 evidence；尚未引入的 pytest-bdd／marker／測試目錄不可寫成已可執行功能。Coverage 不代表 production 可用，既有失敗另行記錄，不接管未核對的正式資料或服務。

Golden Path 必須在真實環境證明：Photos cold read-back 完整性通過 → DURABLE → READY → Jellyfin projection／影音播放／seek → 安全刪 staging → 清播放 cache → 再由 Photos 恢復播放／seek，而且無重複上傳或 quota。local finalize、單段播放或 HTTP HEAD 不足以通過。

每段 promotion 要 contract、隔離 integration、live、故障恢復、rollback 與唯一 execution ownership 通過；critical 情境失敗或證據缺失即阻擋。未實播、未滿 24 小時 quota 觀察或未核實項目維持 OPEN／未驗收。

純文件修改檢查連結、命令語法、BDD 完整性與差異範圍；程式未變且上一輪完整檢查已通過時，不重跑同一套測試。未執行的檢查不得宣稱通過。
