# pixAV

pixAV 將外部媒體來源可靠地轉換成「已驗證、可長期從 Google Photos 恢復播放」的私人媒體資產。Google Photos 是目標架構的持久媒體儲存，本地磁碟承擔 staging 與可重建的播放 cache；PostgreSQL 始終保存 domain 事實。Jellyfin 負責展示、搜尋、收藏、觀看紀錄與各裝置播放。

本文件整合產品的十項核心目標與完整 BDD 規格，集中描述目標、程式現況、行為契約、開發順序及操作方式。[AGENTS.md](AGENTS.md) 定義開發與資料安全規則；[CLAUDE.md](CLAUDE.md) 僅為入口。本文中的「必須」是驗收要求，不表示程式已實作或部署已通過。

## 完成標準

同一個 `video_id` 必須走完：來源選擇 → qBittorrent 下載 → ffprobe 檢查與必要 FFmpeg 準備 → 指定 Pixel-compatible environment 上傳 → 全新 cold path 回載與完整性驗證 → `RemoteAsset.DURABLE` → `PlayableAsset.READY` → `.strm + .nfo + artwork` 發布 → Jellyfin 實播及 seek → 安全刪除 staging → 清空可重建播放 cache 後，再由 Photos 恢復播放及 seek。

全程不能重複上傳或重複計入成功用量。上傳 UI 成功、分享頁 HTTP 成功、local finalize、單段可播、CLI exit 0、coverage 達標，均不足以宣告這條流程完成。未在真實環境穩定通過前，Phase 0 與 production playback 保持未驗收。

## 五個核心邊界

```text
多個外部來源 adapter
        ↓
SourcePolicy → MediaWorkflow → GooglePhotosStorage
                                      ↓
                              PlaybackResolver
                                      ↓
                              LibraryProjection → Jellyfin

PostgreSQL：以上各邊界的 domain facts
唯一 execution authority：活動派送、attempt、timer、retry 與恢復
Redis／播放 cache／library projection：衍生狀態
```

| 核心 | pixAV 維護的決策 | 委派的能力 |
| --- | --- | --- |
| SourcePolicy | normalization、去重、eligibility、品質與政策評分、cooldown、候選切換、provenance | 外部 adapter／indexer 發現候選 |
| MediaWorkflow | execution 生命週期、checkpoint、下載與準備政策、失敗分類、恢復、storage handoff | qBittorrent 執行 torrent；ffprobe／FFmpeg 檢查、remux 及必要轉換 |
| GooglePhotosStorage | 帳號 LRU、quota、cooldown、lease、上傳 reconciliation、remote reference、cold read-back、durability、刪除資格 | 指定 Pixel-compatible environment 執行上傳；Google Photos 儲存媒體 |
| PlaybackResolver | 穩定 URL、播放資格、遠端重新解析、cold materialization、Range／seek、分段完整性、cache 與 reader 保護 | 經驗收的 OSS 傳輸能力；播放器負責解碼 |
| LibraryProjection | effective metadata、`.strm`／`.nfo`／artwork、原子發布、reconciliation、撤架與重建 | Jellyfin 掃描、展示、搜尋、收藏與觀看狀態 |

這是業務邊界，並不要求立刻改成五個獨立服務或更名現有目錄。不自行實作 torrent、codec、播放器、transcoder、通用 crawler、queue/DLQ/retry framework、UI automation framework、media proxy 或 catalog/search framework。PlaybackResolver 保留 Photos 專屬政策與穩定入口，傳輸實作以成熟元件的已驗證契約承接。

不預選 orchestrator；選定後只有一套 execution SSOT。upload concurrency 固定為 1。不並行維護 Stash／MetaTube 兩套 metadata 系統，不安排 Rust rewrite。現有 fallback 必須等完整 Phase 0 通過，再按各自替代契約逐段退休。

## Domain 與狀態契約

下列為目標模型；目前 schema 對應與缺口見後文。不要把來源、執行、遠端儲存、播放、發布狀態全部塞進 `Video.status`。

| Entity | 身分與事實 | 狀態／約束 |
| --- | --- | --- |
| Video | 穩定 `video_id`、媒體身分、metadata 與 provenance | 候選故障不得污染媒體身分；不同 info hash 不代表已證明相同媒體 |
| SourceCandidate | 所屬 Video、provider identifier、正規化欄位、排名、失效證據 | hard eligibility 優先於分數；來源 cooldown 可恢復 |
| Execution | domain task 與各次 attempt、owner、外部 operation reference、checkpoint、due time | `READY / RUNNING / WAITING_RETRY / WAITING_QUOTA / USER_ACTION_REQUIRED / SUCCEEDED / FAILED / CANCELLED` |
| RemoteAsset | provider=`google_photos`、account、可重新取得媒體的 reference、預期內容、驗證證據 | `REQUESTED → CREATED → VERIFIED → DURABLE`；全段冷回載成功即提交 `VERIFIED`，manifest 整體通過才 `DURABLE`。完整性失敗不得 durable；已 DURABLE 的資產重新驗證失敗才轉 `INVALID`，share URL 過期不是失效 |
| PlayableAsset | RemoteAsset／manifest 版本、可播放物件、完整性與播放驗證 | `PREPARING → READY`；過期準備為 `STALE`，不可恢復為 `INVALID` |
| Account | LRU、quota 使用與重設、cooldown、lease ownership | DB clock 決定資格；lease 覆蓋實際執行且不可並行擁有 |
| LibraryPublication | video、projection 版本、有效 metadata、必要檔案與發布結果 | `PENDING / PUBLISHED / STALE / FAILED`；只有 READY 可發布 |

Models immutable；外部 payload 在 adapter 邊界驗證。metadata 以 provider namespace 合併並保留 provenance；manual override 優先於 scraper，refresh 不得覆蓋人工值。未知的演員、studio、backdrop 等 optional 欄位不阻擋發布，也不捏造內容。

短命 CDN URL 只能在 TTL cache，不能作永久 identity。`share_url`／provider reference 只是重新解析的線索，其存在不證明內容 durable。URL 到期不應使已驗證 RemoteAsset 失去 durability；確認遠端內容毀損或遺失，才走相應 invalidation 與恢復。

每個有外部副作用的 business operation 必須有可辨識的穩定鍵、intent 與 reconciliation 依據。crash 後先確認 qBittorrent、輸出檔或遠端上傳是否已成功，再決定重做。無法證明是否上傳成功時，保留本地檔並進入 reconciliation／人工處理，不能以盲目重送換取表面成功。確認遠端建立與成功用量應以可重入的同一交易提交；重試 commit 不重扣，未確認的副作用不得直接當成零用量重傳。

持久 retry／quota 用 PostgreSQL clock；heartbeat 間隔用 monotonic clock。worker 回報活動結果，唯一 execution authority 決定有限 retry 與 timer。terminal failure 不自動 replay；人工 replay 建立可追蹤的新 attempt／execution，保留原始歷史。

## 五塊核心的驗收要求

### SourcePolicy

adapter 回傳合法候選才進入正規化；錯誤 payload 記錄 adapter error，其他候選仍可使用。保留 provider identity，不發明跨來源媒體關聯。同樣輸入與政策版本產生確定的排序；最高分違反硬性條件時選下一個合格候選。

`num_seeds=0` 不能單獨判定永久失效。確認 `SourceUnavailable` 才讓該候選 cooldown 並切換；Redis、網路或 worker 故障走 infrastructure retry，不污染候選。全部來源暫不可用時明確等待來源恢復，不產生假下載；新發現可重新使媒體具備執行資格。

### MediaWorkflow

選定來源後關聯 execution 與 qBittorrent 工作；下載完成記錄 artifact，再由 ffprobe 取得 container、影音 codec、解析度及 duration。媒體無法解析就拒絕 upload-ready。相容格式跳過不必要轉碼；remux 後重新 probe，duration 必須在明確容許誤差內，失敗保留可恢復原始檔。

下載成功但 checkpoint 未提交、remux 輸出完成但 transition 未提交，都須重新驗證既有結果並接續。domain task 與 attempt 分開觀測。候選失效交 SourcePolicy；暫時依賴故障有限重試，耗盡後 terminal。保留 FIFO、in-flight recovery、後段處理與 single-flight 的既有安全語意。

### GooglePhotosStorage

以 LRU 在 quota 足夠、未 cooldown 的帳號中取得 lease，執行中維持 ownership，終止或失聯後安全釋放／過期。沒有合格帳號時 `WAITING_QUOTA` 並保留 staging。指定環境不可用就失敗或等待，不得默默改走一般直接 upload。rclone fallback 若使用，必須明確計入 quota，不能宣稱具有同樣的儲存或配額效果。

登入 challenge 進 `USER_ACTION_REQUIRED`，停止自動提交帳密。上傳建立 RemoteAsset 後，必須使用新的 retrieval session、空的回載 cache，且不掛載可冒充遠端回載的原始下載或上傳片段。驗證政策要記錄版本、預期 byte count、duration 誤差、codec 及必要 hash；完整原件驗證要求全量 byte count／SHA-256 一致。若未來允許 provider 轉換，必須另定且實測完整性政策，不可在 hash 失敗時臨時放寬。

只有 remote creation、cold read-back 與政策檢查都成功，才能提交 `DURABLE`。單次成功不保證未來永遠可取回；後续失效、定期重新驗證的觸發條件與恢復策略也須有可觀測狀態。分段 manifest 完整且不可變，每段有身分、順序、時間範圍、size、hash、codec 與 account/reference；所有段及交界通過才能代表整片。

### PlaybackResolver

Jellyfin 永遠使用 `video_id` 的 pixAV 穩定入口，例如 `/stream/{video_id}`。目標契約不得把短命 provider URL 寫入 projection、回 JSON 或以對外 redirect 暴露給 Jellyfin；需要的傳輸能力由經驗收的 OSS 整合提供，pixAV 不擴寫通用 proxy。

只有 durable 遠端內容且準備成功才可 `READY`。完整 GET 的 byte count／SHA-256 要符合可播放物件；HEAD 不傳 body，已知時回正確 Content-Length，不為取得長度無謂全量下載。single Range 驗收首段、中段、suffix 的 206／Content-Range／精確 bytes，以及越界 416。seek 不應為略過內容強制重傳前面全部 bytes，附近重複 seek 可利用 cache。

cold path 可 materialize 暫存內容；首次準備成本須與 READY 後的 Range 效能分開驗收，不能用「整片先下載完才 seek」證明 cold seek 已達標。cache 可丟棄並由遠端重建，TTL 到期仍須等 active reader 釋放。缺段、只完成第一段或 manifest 版本不符都拒絕整片播放；分段交界必須驗證畫面、聲音與 seek。

### LibraryProjection

只有 `PlayableAsset.READY` 可發布；RemoteAsset durable 本身不夠。輸出穩定 URL 的 `.strm`、Jellyfin-compatible `.nfo` 與有效 artwork；metadata 含可用的 title、description、release、演員、studio、tags。已保留的封面不得因外部 provider 暫時故障而消失，backdrop 可缺省。

準備全部必要檔案後才原子啟用完整版本，更新失敗保留先前有效發布，不能露出半寫檔案。重跑不產生重複 library item。metadata 變更可刷新，缺檔與整個生成目錄遺失可從 PostgreSQL 的發布資料及保留的 artwork 原件重建；DB 保存 artwork 的 provenance/reference，保留的原件也納入備份。Jellyfin DB 遺失不影響 domain facts。

PlayableAsset 變 INVALID 後撤下／停用 active projection；撤架不刪遠端資產。Jellyfin 驗收包含掃描、封面、tags 導覽／搜尋或篩選、實際 client 播放、seek 與續播。收藏、觀看紀錄與裝置體驗由 Jellyfin 管理，其個人狀態備份由 Jellyfin 部署負責，不能宣稱只靠 pixAV DB 可還原觀看紀錄。

## Cleanup 與存取安全

staging 的刪除資格必須同時成立：`RemoteAsset.DURABLE`、完整性證據有效、`PlayableAsset.READY`、播放驗證通過、完整 library publication、retention 到期、沒有 active execution／reader，以及 path、symlink、ownership 與操作備份守衛通過。disk 壓力或 retry 耗盡不取代這些條件；`pixav-local://` 媒體不是遠端備份。

資格檢查與真正 unlink 之間必須防止 reader／execution 新增或版本變更造成競態。dry-run 不改 DB、queue 或檔案；apply 僅刪當次核准的 exact target/count，不能以擴大 glob 取代目標。刪除成功記 audit、更新 local reference；crash 後重查檔案系統並 reconciliation，不能把 missing file 當成遠端完整的證據。

cache eviction 與 staging cleanup 有不同用途，但都保護 active readers。private playback 由部署的認證與授權邊界保護；未授權 client 與已撤銷裝置的新請求必須被拒絕。projection、log、model repr、metrics、fixture、Maestro YAML 不得洩漏 credential、cookie 或 Photos token。不要把目前 HTTP route 的存在當成已實作 private access control。

## Repository 現況與差距

以下依本次工作區程式、callers、migration、Compose 與測試內容核對；未連線認證正式 DB／Redis、未重跑 live contract，也未宣告任何部署通過。測試檔存在只表示有對應檢查，並不代表本次執行成功。工作區已有未提交程式修改，本文描述的是該工作區，並非特定 release。

| 邊界 | 可確認的實作依據 | 尚需補齊與證明 |
| --- | --- | --- |
| SourcePolicy | [policy](src/pixav/sht_probe/policy.py)、[discovery caller](src/pixav/sht_probe/service.py)、[authority](src/pixav/maxwell_core/media_workflow.py)：provider observation、hard eligibility、確定排序、DB-clock cooldown 與候選切換 | managed 模式 opt-in；provider live access／parity 與 production promotion 仍需當次 evidence |
| MediaWorkflow | [authority](src/pixav/maxwell_core/media_workflow.py)、[activity worker](src/pixav/media_loader/activity.py)、[preparation](src/pixav/media_loader/preparation.py)、[014 migration](migrations/014_media_workflow_recovery.sql)：execution／attempt 分離、DB clock retry、有限失聯恢復、operation intent、原始檔 hash 綁定的 lossless-mp4-v1 準備 | 隔離 contract 與 production 驗收分開；legacy orchestrator 僅服務未 admit 的 video，不自動接手受管 execution |
| GooglePhotosStorage | [storage authority](src/pixav/maxwell_core/storage_workflow.py)、[activity worker](src/pixav/pixel_injector/storage_activity.py)、[Photos adapter](src/pixav/pixel_injector/photos_storage.py)、[repository](src/pixav/shared/remote_assets.py)、[013](migrations/013_remote_storage.sql)／[017 migration](migrations/017_remote_asset_reverification.sql)：execution-owned account lease、WAITING_QUOTA、USER_ACTION_REQUIRED、實際提交的 `REQUESTED→CREATED→VERIFIED→DURABLE`，以及 [versioned policy registry](src/pixav/shared/storage_models.py)：`photos-original-v1` 凍結保留、`photos-original-v2` 為現行版本，除全量 hash 外要求回載收據帶 ffprobe 觀測事實，並在促成前以 `segment_plan` 的同一判準重驗持久化 manifest 的時間軸；`reverify_due` 依 `PIXAV_REMOTE_REVERIFY_INTERVAL_DAYS`（預設 30，0 停用）開只走 verify stage 的 execution，重新驗證失敗才 `invalidate`；[segment staging](src/pixav/pixel_injector/segment_staging.py) 以硬連結呈現 canonical 檔名，[retained guest](src/pixav/pixel_injector/managed_runtime.py) 與 [015 migration](migrations/015_managed_runtime.sql) 將 guest 建立記為可 reconciliation 的 intent，帳號切換退休 runtime 而非共用裝置 | 真實 Pixel-compatible 環境、真帳號 cold read-back 與 24 小時 quota 觀察尚未實測；contract 測試使用 fake，不能作 promotion 證據。回載收據形狀屬容器契約：`photos-original-v2` 的資產必須由重建過的 [storage-tools](docker/storage-tools.Dockerfile) 映像回載，舊映像產出的收據會被拒（刻意 fail-closed） |
| 分段隔離流程 | [010](migrations/010_video_parts.sql)、[011](migrations/011_first_4k_heartbeat.sql)、[video_parts](src/pixav/shared/video_parts.py)、[單片 CLI](scripts/first_4k_movie.py)：manifest、checkpoint、usage marker、heartbeat 與 cold preparation | 隔離設計與一般 worker 尚有差異；需實測完整 Photos／24 小時 quota／Jellyfin／刪 staging 後再播，不能直接 promotion |
| PlaybackResolver | [routes](src/pixav/strm_resolver/routes.py)：`/resolve` 回 CDN JSON、`/stream` 回 302、`/local` 支援 GET／HEAD／single Range；分段檢查 prepared manifest | 對外隱藏 provider URL、完整 durability gate、自動 cold cache rebuild、reader 保護及正式 client 契約尚未齊備 |
| LibraryProjection | [generate_strm](src/pixav/strm_resolver/strm_generator.py) 寫穩定 stream URL；[測試](tests/strm_resolver/test_strm_generator.py) 驗證生成 | 尚無完整 `.nfo + artwork` 原子 publication／reconciliation；base Compose 未提供 Jellyfin service |
| Cleanup | [janitor](src/pixav/maxwell_core/gc.py) 檢查 `verified_playback_evidence`、`exact_target_backup_authorization`、RemoteAsset、publication、open execution、reader lease、DB-clock retention 與 path／symlink containment；後者要求 [018](migrations/018_cleanup_authorization.sql) 的未過期授權列，其 path 與 janitor 當場重算的 sha256／size 相符且指向本 video 的 DURABLE asset；dry-run 不寫 DB／audit／檔案，apply 拒絕時寫 audit | `verified_playback_evidence` 需要 Component D 的 `playable_assets`，該表尚未存在，故此條件在 production 恆為 false——這就是被具名的阻擋依賴，不再是硬編碼常數。DURABLE 與 publication 資料列不足以開啟 gate，BDD-058／063／134 正向路徑保持 OPEN |
| Domain | [models](src/pixav/shared/models.py)、[storage models](src/pixav/shared/storage_models.py) 有 Video、Task、Account、SourceCandidate、VideoPart、RemoteAsset、RemoteAssetSegment；[migrations](migrations/) 為 001–018 | PlayableAsset 與 LibraryPublication 尚未成為正式模型；既有 frozen models 不等於所有 secret repr 已合格 |

[單元測試](tests/)、[真實 repository／migration 測試](tests/integration/)、[segmented HTTP 測試](tests/strm_resolver/test_segmented_routes.py) 與 [E2E 入口](tests/e2e/) 可作後續基礎。`pyproject.toml` 尚無 pytest-bdd dependency 或 `tests/bdd/`；BDD 情境索引目前是規格，不是已可執行的 suite。

## 開發順序與交付門檻

| 順序 | 可審查的交付 | 通過條件 |
| --- | --- | --- |
| 1. 保護現有 artifact、建立事實模型 | 先補一般 cleanup 的完整拒絕條件；新增 domain 模型／migration／repository，定義驗證政策與 operation identity | 真 PostgreSQL 測 transition、constraint、舊 schema 對應、競態與 restore；既有 AVAILABLE／share URL 不自動升級為 DURABLE |
| 2. 打通隔離 Golden Path | 以合成媒體、單帳號、upload concurrency 1，串接五塊核心；完成 Photos cold read-back、READY、原子 projection 與 Jellyfin | 全 GET／hash、HEAD／Range、影音／seek、刪 staging 且清 cache 後再播；無重複 upload／quota |
| 3. 證明故障恢復與安全 | 注入 upload 後 commit 前、verification 後 cleanup 前、unlink 中、URL expiry、Redis／projection 遺失等失敗 | 副作用 reconciliation、有限 retry、人工 replay audit、reader guard、private access 與精確備份還原通過 |
| 4. 選定 execution 與替代 adapter | 以相同 BDD 比較成熟 OSS，確認 ownership、timer、FIFO、single-flight、後段優先與恢復 | 完整 Phase 0 先過；新舊 executor 不得同時寫同一 execution，保留 domain facts 並完成 rollback |
| 5. 分段 canary 與退休 fallback | 各 adapter／execution 邊界有版本、health／metrics、secret、volume、資源限制、備份及 rollback 契約 | contract、隔離 integration、live、recovery、rollback 全過；任何 critical 情境失敗或無 evidence 均阻擋 promotion |

不以「更換 orchestrator」代替 cold durability 與 remote-only playback 的驗收。候選 execution 工具可比較 Taskiq／Windmill／Temporal，尚未選定；來源 adapter 替代須通過 access-gate、bare-hash-only、同範圍非空 parity，才能退休既有 fallback。Maestro、MediaFlow、MetaTube 等僅為待驗證的替代整合選項，不是目前已接管的服務。任何替代都不能遺失來源政策、帳號政策、Photos resolver、projection 或 cleanup guard。

每次交付在既有測試目錄加入能驗證行為的測試；需要 pytest-bdd 時才新增工具與 `tests/bdd/features/`、`steps/`、`fixtures/`，按五個核心及 system／Jellyfin 分組。保留本文件情境 ID 作 traceability，不建立另一份 roadmap 或測試報告作長期規格。

## 驗證層級與證據

| 層級 | 要驗證的邊界 | 限制 |
| --- | --- | --- |
| contract | 純 domain／adapter contract，以已實測契約建立 fixture | fake Photos 不證明 live upload 或 cold recovery |
| isolated | 專用 Compose、真 PostgreSQL／Redis／qBittorrent 及所需 Jellyfin；外部副作用可用經驗證 fixture | 先核對 instance；不能使用正式 queue |
| live | 指定 upload environment、專用 Google account、Photos、cold reload 與真實播放器 | 需當次版本、identity、完整性與播放 evidence |
| production canary | production-like 單片完整 Golden Path，加 recovery／rollback | 完成前不得標 Phase 0／production playback PASS |

原 BDD 的 `@contract / @isolated / @live / @production_canary` 是設計層級，`@critical / @destructive / @chaos / @security / @jellyfin / @promotion` 表示驗收屬性；不是目前 pytest 已註冊的全部 markers。現有 `integration`／`e2e_live` 是 opt-in，設定與 instance guard 見 [conftest](tests/integration/conftest.py) 及 [integration_guard](scripts/integration_guard.py)，不要只設啟用旗標就對未知服務執行。

程式變更執行 [AGENTS.md 的完整檢查](AGENTS.md#驗證)。live evidence 要能關聯 revision／image、DB identifier/database、Redis run ID、execution／video、manifest／verification version、驗證時間及結果；敏感 reference 與媒體保存在私有 evidence，不放本文件。PASS 要有實測，缺資料、等待 24 小時 quota 或未實播保持 OPEN／未驗收。模擬規則測試、container healthy 或過去主機的數字均不能代替當次 runtime evidence。

故障恢復至少包含：upload 成功但 DB 未提交時不重傳且只扣一次；verification 後 crash 保留 durable facts；unlink 中 crash 安全收斂；Redis 遺失不遺忘遠端資產；Jellyfin／projection 遺失可重建；播放途中 URL 到期可重新取得。監控要區分 source-unavailable、WAITING_QUOTA、upload failure、remote verification failure、publication lag，並保留 queue／retry／DLQ、disk／pause 與實際播放信號。

## 部署與資料操作

目前 repository 使用 Python 3.10+、uv、FastAPI、PostgreSQL、Redis 與 Docker Compose。先按 [.env.example](.env.example) 建立私有 `.env`、配置帳密、VPN 與所需私有檔。`PIXAV_PIXEL_INJECTOR_MODE` 預設 `redroid`；明確設 `local` 才是 local finalize，該模式保留本地媒體，不滿足遠端 durability。managed 上傳只在 `redroid` 且 `PIXAV_MANAGED_MEDIA_WORKFLOW=true` 時啟動；`PIXAV_STORAGE_STAGING_DIR` 是 guest 唯讀掛載的 staging root，`PIXAV_STORAGE_GUEST_DATA_DIR` 是 guest 的 `/data`，兩者都不得指向下載目錄或播放 cache。

```bash
uv sync
docker compose config -q
docker compose -f docker-compose.yml -f docker-compose.prod.yml config -q
docker context show
docker info --format 'daemon={{.Name}} id={{.ID}}'
docker compose ps -a
docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT system_identifier, current_database() FROM pg_control_system();"'
docker compose exec -T redis redis-cli INFO server
```

以上 identity 查詢不構成 apply 授權。新環境需先建立專用 DB／Redis 才能查 identity；既有環境先確認 Docker daemon/context，不以 localhost、container name 或歷史值作證明。production 使用同一組 `-f` 核對及操作；不要在不同 Compose 組合間混用 identity。

[migrate](docker/migrate.Dockerfile) 會執行 pending migration，worker 以其成功作啟動依賴。008 新增來源候選、009 移除 CDN 欄位、010／011 新增分段與 heartbeat、012 建立 managed execution 與其資料庫角色、013 建立 RemoteAsset、account lease、reader lease 與 cleanup audit；014 補上失聯恢復記錄、自訂 lease 與舊 task 的 ownership 守衛；015 把上傳 guest 的建立記成 journal，crash 後由人工判定而非重建；016 只補上 activity worker 實際執行的 accounts 讀取；017 加上 `remote_assets.reverified_at` 與到期索引，並把每段都已冷回載卻停在 CREATED 的資產推進到 VERIFIED（只依資料庫既有事實，不新增判斷）；018 建立 `cleanup_authorizations`，授權對象是一個 exact target 而非 glob，且只授予 execution authority 讀取權。升級前停會推進狀態的 worker，核對相容性、同實例 full backup／sidecar 與 restore；舊 process 不可跨不相容 schema 繼續執行。完成必要驗收後才部署，rollback 使用已驗證 restore 與相容映像，不手寫反向 SQL、不在正式環境 `down -v`。

[base Compose](docker-compose.yml) 的 qBittorrent 共用 gluetun network namespace，VPN kill switch 必須以 tunnel failure 實測阻流。`vpn_sentinel` 與 loader 使用相同 `PIXAV_VPN_EGRESS_ECHO_URL`，出口 IP 比對只告警，過期為 unknown。WebUI 預設 8085、indexer 9117、browser helper 8191、resolver 8000、worker health 8001–8005（`storage_worker` 為 8005）；`stash`、`monitoring` 為 optional profiles。production override 的存在不代表版本/license、資源限制或安全 gate 全部已合格。

Cookie 用 `PIXAV_CRAWL_COOKIE_FILE` 指向私有檔案；`.env`、`secrets/`、`backups/` 不進版控。備份目錄 0700、檔案建立即 0600，離機加密保存；dump 可能含 credential。備份須核對 identity sidecar、`pg_restore --list`，並定期隔離 restore；execution 更換時另保留 Redis volume snapshot 及恢復契約。

| 操作入口 | 必要守衛 |
| --- | --- |
| [backup_postgres](scripts/backup_postgres.py) | 預設 container 是 `pixav-postgres`，必須另核對 instance；`--output` 指向本次私有備份 |
| [manage_system_pause](scripts/manage_system_pause.py) | `status/pause/resume`；只以自己的 `--token` 解除 pause，不覆寫他人的 ownership |
| [manage_download_pause](scripts/manage_download_pause.py) | 在正確 network 操作；resume 重查 100 GiB／10% disk latch，不直接刪 key |
| [phase0_backlog](scripts/phase0_backlog.py) | `status/backup-one/run-one/upload-one`；先停會產生或 claim 工作的 worker，核對 full／selected-row／queue backup、exact task/video/head/count |
| [phase0_cohort](scripts/phase0_cohort.py) | `snapshot/sample/mark/report`；固定 T0、私有 evidence；Redis restart 後重核 run ID 與副作用，缺 span 不算通過 |
| [cleanup](scripts/cleanup_watermark_garbage.py)、[replay](scripts/replay_task.py) | 資料清理先 dry-run、exact target/count、可恢復備份；replay 指定 task 並保留操作者、原因及有限 retry cycle audit |
| [manage_execution](scripts/manage_execution.py) | managed execution 專用 `list/inspect/replay/cancel/resume/retry-now/reopen`，另有 `invalidate-asset`（對象是 RemoteAsset，不是 execution）；寫入前印出 DB identity，`--db-identity` 不符即中止。除 `list/inspect` 外全部必填 `--operator` 與 `--reason`，只輸出 Execution／ExecutionAttempt 的分類與識別碼，不印 checkpoint 或任何 credential。`resume` 只能清除 credential guard，記錄遠端效果的 recovery fact 一律拒絕；`invalidate-asset` 用於操作者自己確認遠端副本已消失，share URL 解析失敗不是這件事 |
| [canary_upload](scripts/canary_upload.py) | 把操作者手上的檔案以 `operator-supplied` provenance 送進受管 pipeline（`admit`）並觀察（`status`）；download stage 只接受 40-hex info_hash，這是唯一合法入口。重新計算 hash 不採信宣告值，輸出不含 credential、share location 或本機路徑 |
| [provision_workflow_roles](scripts/provision_workflow_roles.py) | 建立／授予 `pixav_execution_authority` 與 `pixav_activity_worker` 的登入角色；credential 只從環境取得，不寫入 SQL 或 evidence |

完整參數使用 `uv run python <script> --help`，其中 `<script>` 替換為表中的實際路徑。`run-one --apply` 要求 full 模式且尚無 local_path；下載完成只 route task。`upload-one --mode local --apply` 要重新取得 upload stage 的 full／selected backup，不沿用 download 快照；它仍不能驗證 Photos。解除 owned pause 的操作必須有失敗恢復 pause 的保護，resume 失敗就停止。

### SourcePolicy → MediaWorkflow 的 managed 接口

`PIXAV_MANAGED_MEDIA_WORKFLOW` 預設 false。開啟前完成 012–018 migration／restore 驗證、停止同 cohort 的舊 admission 並 reconciliation。Maxwell 使用獨立非 superuser login，隸屬 `pixav_execution_authority`；media activity worker 使用僅隸屬 `pixav_activity_worker` 的另一 login。Compose 透過 `PIXAV_MAXWELL_DB_USER/PASSWORD`、`PIXAV_ACTIVITY_DB_USER/PASSWORD` 分別設定；未設定時沿用 legacy DB 設定，但 managed runtime 會拒絕不符角色的連線。credential 僅置於私有設定，不放 SQL 或 evidence。

Source adapter 產出 `source-observation-v1`，保留 provider／provider_id、正規化 torrent identity、title、seeders、size 與安全 provenance。Maxwell 才能建立 domain task；同 hash 保留不同 provider observation，不以同標題合併不同 hash。`PIXAV_SOURCE_MIN_QUALITY_SCORE` 由 discovery 與 authority 共用；hard rejection 優先，排名以分數、provider、provider_id、info hash 確定排序。`num_seeds=0`、不完整回應與 timeout 不單獨證明來源不可用。

`MediaWorkflow.admit/observe/ingest_observation/next_activity/consume_results/recover_expired` 是來源到執行的 Python 接口；`inspect` 回傳 immutable Execution／ExecutionAttempt，不含原始外部 payload。`cancel/replay` 必填操作者與原因；取消保留 artifact，replay 僅接受 FAILED／CANCELLED。`observe_states` 供 tick 匯出 WAITING_QUOTA、source-unavailable、USER_ACTION_REQUIRED 與 terminal 分類的 gauge，讓等待與失敗在監控上分得開。操作入口是 [manage_execution](scripts/manage_execution.py)；舊 `replay_task` 仍只服務 legacy task。資料操作須先核對 instance、exact target 與備份，這些接口不取代操作守衛。

Redis `pixav:media-activity:v1` 僅傳送 `ActivityRequest`；每次包含 task／execution／attempt／operation、owner、generation 與 stage。worker 對照 DB intent，取得單次 claim，只提交 `ActivityResult`，提交成功後 ACK。PostgreSQL 決定 due time；heartbeat 每 15 秒、預設 lease 120 秒。來源 cooldown 沿用 `PIXAV_SOURCE_CANDIDATE_COOLDOWN_HOURS`；retry 使用 `PIXAV_DOWNLOAD_MAX_RETRIES` 與 `PIXAV_RETRY_BACKOFF_SECONDS`。失聯恢復保留 operation identity，超過有限恢復額度進 USER_ACTION_REQUIRED；一般 infrastructure retry 耗盡進 FAILED。下載 pause 不阻止 prepare／storage 後段。

`TorrentClient.reconcile_download` 只接續帶相同 operation tag 的 torrent，未知 ownership 停待處理。`prepare_media` 先 probe 全部串流、計算 SHA-256，接受一條 H.264／HEVC video 及至少一條 AAC／AC3／EAC3／ALAC audio；額外或不支援串流直接拒絕。相容 MP4 直接使用，其餘 stream-copy remux；保留全部支援音軌，duration 誤差最多 0.05 秒。ffprobe 預設 timeout 30 秒、FFmpeg 600 秒，timeout／取消會回收 subprocess。輸出及 0600 intent 存於 operation 專屬目錄，重用時須核對輸入 hash、政策與重新 probe 結果；沒有 intent 的舊輸出不自動升級。來源、準備輸出及 execution facts 都保留，不因失敗刪原檔。

目前工作樹已有 storage authority，準備完成後以同一 Maxwell execution 交易建立 REQUESTED RemoteAsset、更新 checkpoint 並進入 upload stage；不再派發可競寫的 legacy upload task。這是持久交接，不是 DURABLE／READY。既有 storage worker 的部署、Photos cold read-back、quota 及後續 Golden Path 分別驗收。本次不引入新 orchestrator／第三方服務或退休 fallback；工具與映像沿用既有 Compose。rollback 先停新派送、fence owner、reconciliation，再以相容程式讀取保留的 PostgreSQL／artifact；不得把受管 execution 直接交給舊 worker。

| BDD 範圍 | 行為驗證入口 |
| --- | --- |
| 007–018 | [policy tests](tests/sht_probe/test_policy.py)、[真 PostgreSQL workflow tests](tests/integration/test_media_workflow.py)：normalization、provider identity、排序、cooldown、耗盡／恢復 |
| 019–024、031–035 | [activity tests](tests/media_loader/test_activity.py)、[workflow tests](tests/integration/test_media_workflow.py)：ownership、attempt、retry、取消、checkpoint、重複回報與 Redis 恢復 |
| 025–030、035 | [preparation tests](tests/media_loader/test_preparation.py)、[合成 FFmpeg contract](tests/integration/workflow_media_contract.py)：完整 probe、無損準備、原檔保留與輸出重用 |
| 024、034 | [隔離 torrent contract](tests/integration/test_workflow_torrent.py)：專用無外部出口 network、合成 torrent、operation ownership 與已完成 artifact 重用 |
| 004–005、130–133 | [cleanup refusal tests](tests/integration/test_cleanup_gate.py)：證據不足拒絕、dry-run 零資料變更；不代表正向刪除／播放已通過 |
| 008、015 | [service tests](tests/sht_probe/test_service.py)、[worker tests](tests/maxwell_core/test_worker.py)：壞掉的 provider 列被計入 `pixav_source_adapter_errors` 且不影響其餘候選；[workflow tests](tests/integration/test_media_workflow.py)：infrastructure 失敗不改動 `source_candidates` |
| 036–057、059–062 | [policy tests](tests/shared/test_storage_models.py)、[storage activity tests](tests/pixel_injector/test_storage_activity.py)、[真 PostgreSQL storage tests](tests/integration/test_storage_workflow.py)：account lease／quota、`CREATED→VERIFIED→DURABLE`、v2 整片一致性與 manifest 時間軸、重新驗證與失效 |
| 058、063、134 | 無正向測試，刻意保持 OPEN。[cleanup refusal tests](tests/integration/test_cleanup_gate.py) 只證明兩個具名條件缺一即拒絕 |

上述為可執行測試入口；integration 仍須 opt-in 並通過 instance guard。host 沒有 FFmpeg 時可在既有 media-loader image 以唯讀 repository、獨立暫存目錄及 `--network none` 執行 standalone contract。新 adapter 的 live contract、VPN 失效阻流、完整 rollback 與 production Golden Path 證據未齊前保持 OPEN，coverage 不代替這些驗收。

### 隔離 Photos 與分段驗證

[單片 CLI](scripts/first_4k_movie.py) 使用 [first-4k Compose](docker-compose.first-4k.yml) 的專用 DB／Redis 與 `.verify/first-4k/`。需備妥指定 Android profile／映像、media 工具、Maestro、專用帳號與私有 secret／cookie。該 Compose 的 qBittorrent 直連，不能用作 VPN gate 證據。

```bash
uv run python scripts/first_4k_movie.py --help
uv run python scripts/first_4k_movie.py status
```

`status` 唯讀；`preflight` 會寫 evidence 並核對 instance、mount、image、disk；`boards` 探索來源，`run` 啟動隔離單片，來源資訊只透過私有參數提供。其後沿用原 `--run-id`：

| 命令 | 行為與限制 |
| --- | --- |
| `resume` | 依 PostgreSQL checkpoint 與原設定／映像接續；`--max-parts 1` 限本次新增確認一段，0 不限段數 |
| `prepare-playback` | 全新 Photos cold 回載、逐段 size／hash／codec 驗證、合併及隔離 resolver；不得借用原始下載／上傳片段 |
| `verify` | movie、automation、24 小時 quota 各自判定；未到期保持 OPEN，不把 CLI 成功當 promotion |
| `recovery-drill` | 僅 upload_paused、恰好第一段確認、其餘未動、原 guest/tools running 且 ownership/image/mount 吻合；會重啟容器，已有 drill 不重試 |
| `reset` | 退休隔離 run，可能停止 owned runtime；先核對副作用，不用於繞過恢復守衛 |

完整來源驗證先提交 PostgreSQL 的 `source_validation` checkpoint；resume 重算完整來源 SHA-256 並核對 size、codec signature、duration 及 verification version。已驗證來源可省略重複解碼，但各段與合併物件仍需驗證；未提交的分段不可直接當成已完成 manifest。JSON 只是 evidence，DB 才是 checkpoint 事實。

QUOTA_WAIT 依 DB 時間恢復；STALE 先核 heartbeat、ownership 與外部副作用；USER_ACTION_REQUIRED 停止重送帳密。保留 guest/checkpoint 供恢復。HTTP 驗收完整 GET/hash、HEAD、首／中／suffix Range、416；headless VLC 的 `--no-audio` 只能證明畫面。實際播放器須驗片頭、中段、片尾、分段交界前後的畫面、聲音與 seek，私有 evidence 記版本及 artifact hash。這套隔離 CLI 尚不能取代完整 Jellyfin、cleanup 後清 cache 再播的 Golden Path。

### 監控

[monitoring](monitoring/) 使用 Prometheus／Alertmanager optional profile；Telegram token/chat ID 由私有檔提供。同版映像執行 `promtool check config`、`promtool test rules`、`amtool check-config` 後，另驗 firing／resolved／跨日 heartbeat 實收。health 只代表 readiness／heartbeat，日常須核對 domain metrics 與播放結果：等待與失敗分開的 `pixav_executions_waiting_quota`／`pixav_executions_source_unavailable`／`pixav_executions_user_action_required`／`pixav_executions_terminal`，遠端儲存的 `pixav_remote_assets_durable_total`／`pixav_remote_upload_failures_total`／`pixav_remote_verification_failures_total`，以及 `pixav_source_adapter_errors_total`（provider 列讀不懂）、`pixav_remote_assets_invalidated_total`（已 DURABLE 的副本被收回，最嚴重的一條）、`pixav_remote_assets_due_reverification`（多久沒被重新看過的積壓）。`pixav_local_cleanup_rejections_total` 持續計數是預期的：cleanup 仍然 fail-closed，理由會指名缺的是 `verified_playback_evidence`。

## 完整 BDD 情境索引

以下保留輸入規格的每一個 Scenario、Given／When／Then／And／But 與標籤，以固定 ID 供後續測試對應；各列都是**待證明的規範**，不是 PASS 清單。共計 143 個情境。上文對「cold path」「不暴露 provider URL」及完整 cleanup 門檻的定義同時適用；端到端情境中任何簡寫不豁免全域安全條件。

### 2. Global Invariants

| ID／情境 | Given／When／Then 契約 |
| --- | --- |
| BDD-001 · PostgreSQL remains the domain source of truth<br>@critical | Given Redis, Jellyfin and execution infrastructure contain derived state<br>When any derived state conflicts with PostgreSQL domain facts<br>Then PostgreSQL domain facts win<br>And derived state must be reconciled |
| BDD-002 · A remote URL is not permanent identity<br>@critical | Given Google Photos returns a temporary playback or CDN URL<br>When the URL later expires<br>Then the video remains resolvable by video_id<br>And the expired URL is refreshed<br>And the expired URL is not treated as durable domain identity |
| BDD-003 · External side effects are idempotent<br>@critical | Given an external operation may already have succeeded<br>When an execution is retried after a crash<br>Then pixAV reconciles existing external state before repeating the operation<br>And the same business operation is not applied twice |
| BDD-004 · Local media is not removed before remote durability<br>@critical | Given a local staging file exists<br>And Google Photos upload has not reached durable verified state<br>When cleanup evaluates the file<br>Then the local file is preserved |
| BDD-005 · Incomplete projection blocks destructive cleanup<br>@critical | Given a remote asset is durable<br>But playback or library projection is incomplete<br>When destructive cleanup evaluates the artifact<br>Then cleanup is rejected |
| BDD-006 · Database time controls durable timing<br>@critical | Given a retry or quota wait is scheduled<br>When its due time is calculated<br>Then PostgreSQL time is authoritative<br>And local process wall clock does not determine durable eligibility |

### 3. Component A — SourcePolicy

| ID／情境 | Given／When／Then 契約 |
| --- | --- |
| BDD-007 · Provider output becomes a normalized candidate<br>@contract | Given a source adapter returns a valid result<br>When SourcePolicy normalizes the result<br>Then a SourceCandidate is created<br>And provider-specific fields are preserved as provenance<br>And scoring fields use the normalized schema |
| BDD-008 · Malformed provider output is rejected<br>@contract | Given a source adapter returns an invalid payload<br>When normalization runs<br>Then no valid SourceCandidate is created<br>And the adapter error is recorded<br>And other candidates remain usable |
| BDD-009 · Provider-specific identity is preserved<br> | Given two providers expose different identifiers<br>When their results are normalized<br>Then each provider identifier remains available<br>And pixAV does not invent an unverified common media identity |
| BDD-010 · Highest eligible candidate wins<br> | Given several normalized candidates exist<br>And every candidate satisfies hard eligibility rules<br>When scoring executes<br>Then the highest ranked candidate is selected |
| BDD-011 · Hard rejection overrides score<br> | Given a candidate has the highest numeric score<br>But it violates a hard eligibility rule<br>When selection executes<br>Then the candidate is rejected<br>And the next eligible candidate is considered |
| BDD-012 · Stable input produces stable ranking<br> | Given the same normalized candidates<br>And the same scoring configuration<br>When scoring runs repeatedly<br>Then candidate ordering is deterministic |
| BDD-013 · Zero seeds alone does not prove permanent death<br>@critical | Given a candidate reports zero current seeds<br>And no independent source-unavailable evidence exists<br>When availability is evaluated<br>Then the candidate is not marked permanently dead solely from seed count |
| BDD-014 · Confirmed unavailable source enters cooldown<br> | Given the selected candidate returns a classified SourceUnavailable failure<br>When failure policy executes<br>Then that candidate enters cooldown<br>And another eligible candidate may be selected |
| BDD-015 · Infrastructure failure does not poison candidate<br> | Given a candidate request fails because Redis, network or a worker is unavailable<br>When failure classification executes<br>Then the candidate is not marked unavailable<br>And the execution follows infrastructure retry policy |
| BDD-016 · Alternate source is selected after source failure<br> | Given source A is selected<br>And source A becomes unavailable<br>And source B remains eligible<br>When SourcePolicy handles the failure<br>Then source A enters cooldown<br>And source B becomes the next selected source |
| BDD-017 · No candidate remains<br> | Given every known candidate is temporarily unavailable<br>When source selection runs<br>Then the workflow becomes source-unavailable<br>And no fake download task is created |
| BDD-018 · New discovery may recover a source-unavailable video<br> | Given a video currently has no usable source<br>When a later discovery adds a new eligible candidate<br>Then the video may become eligible for execution again |

### 4. Component B — MediaWorkflow

| ID／情境 | Given／When／Then 契約 |
| --- | --- |
| BDD-019 · Only one execution authority advances execution<br>@critical | Given a task is owned by the active execution system<br>When a legacy worker sees the same task<br>Then the legacy worker cannot advance execution state |
| BDD-020 · Worker does not schedule its own retry<br>@critical | Given an activity fails with a retryable error<br>When the worker reports the failure<br>Then the execution authority determines retry timing<br>And the worker does not independently enqueue a duplicate retry |
| BDD-021 · Domain task and execution attempt are distinct<br> | Given one domain task has already had two failed attempts<br>When a third attempt starts<br>Then the same domain task remains<br>And a distinct execution attempt is observable |
| BDD-022 · Selected source starts download<br> | Given an eligible source has been selected<br>And the execution is ready<br>When MediaWorkflow invokes the torrent adapter<br>Then the download is associated with the execution<br>And execution state becomes RUNNING |
| BDD-023 · Download completes<br> | Given qBittorrent reports completion<br>When MediaWorkflow receives the completion<br>Then the resulting file path is recorded as an execution fact<br>And the workflow proceeds to media inspection |
| BDD-024 · Worker crashes during active download<br> | Given qBittorrent still contains the active torrent<br>And the worker crashes<br>When execution resumes<br>Then pixAV reconciles qBittorrent state<br>And does not add a duplicate torrent unnecessarily |
| BDD-025 · ffprobe records media facts<br> | Given a completed download exists<br>When ffprobe succeeds<br>Then container is recorded<br>And video codec is recorded<br>And audio codec is recorded<br>And width and height are recorded<br>And duration is recorded |
| BDD-026 · Invalid media is rejected<br> | Given a completed file cannot be parsed as expected media<br>When ffprobe validation runs<br>Then media preparation fails<br>And the file is not promoted to upload-ready |
| BDD-027 · Compatible media avoids unnecessary conversion<br> | Given ffprobe reports a supported container<br>And codecs require no transformation<br>When preparation policy evaluates the media<br>Then unnecessary transcoding is skipped |
| BDD-028 · Container requires normalization<br> | Given ffprobe reports a container requiring remux<br>When preparation runs<br>Then FFmpeg remuxes the media<br>And the resulting file is probed again |
| BDD-029 · Remux does not silently alter expected duration<br> | Given a source duration is known<br>When remux completes<br>Then the output duration is within the configured tolerance |
| BDD-030 · Failed remux preserves recoverable source<br> | Given the original downloaded artifact exists<br>When FFmpeg fails<br>Then the original recoverable artifact is preserved<br>And execution follows retry policy |
| BDD-031 · Infrastructure failure is retryable<br> | Given an activity fails because a dependent service is temporarily unavailable<br>When failure is classified<br>Then execution becomes WAITING_RETRY<br>And a finite retry is scheduled |
| BDD-032 · SourceUnavailable follows source policy<br> | Given the selected source is classified unavailable<br>When download fails<br>Then generic infrastructure retry is not blindly used<br>And SourcePolicy is asked for candidate fallback |
| BDD-033 · Retry limit is exhausted<br> | Given an execution has reached its configured retry limit<br>When another retryable failure occurs<br>Then execution becomes FAILED<br>And it is not automatically replayed forever |
| BDD-034 · Crash after download before checkpoint commit<br>@critical | Given qBittorrent completed the download<br>But pixAV crashed before committing the completion fact<br>When execution resumes<br>Then qBittorrent is reconciled<br>And the existing downloaded artifact is reused |
| BDD-035 · Crash after remux before workflow transition<br>@critical | Given FFmpeg already produced a valid output<br>But execution crashed before advancing state<br>When workflow resumes<br>Then the existing output is verified<br>And it is not remuxed again unnecessarily |

### 5. Component C — GooglePhotosStorage

| ID／情境 | Given／When／Then 契約 |
| --- | --- |
| BDD-036 · Eligible account is selected<br> | Given several upload accounts exist<br>And some accounts have available daily quota<br>And some accounts are not cooling down<br>When account allocation runs<br>Then one eligible account is leased according to scheduling policy |
| BDD-037 · Exhausted account is not selected<br> | Given an account has exhausted its daily quota<br>When upload allocation runs<br>Then that account is not selected before quota reset |
| BDD-038 · Cooldown account is not selected<br> | Given an account is in cooldown<br>And cooldown has not expired<br>When allocation runs<br>Then that account is not selected |
| BDD-039 · No account is available<br> | Given all configured accounts are unavailable<br>When upload allocation runs<br>Then execution becomes WAITING_QUOTA or equivalent wait state<br>And the media remains locally preserved |
| BDD-040 · Account lease covers actual execution<br>@critical | Given an account is allocated to an upload execution<br>When upload is still in progress<br>Then the lease remains owned by that execution |
| BDD-041 · Successful terminal execution releases lease<br> | Given an upload finishes successfully<br>When completion is committed<br>Then the account lease is released |
| BDD-042 · Failed execution releases or expires lease safely<br> | Given an upload worker crashes<br>When its lease expires without a heartbeat<br>Then another execution may eventually acquire the account<br>And concurrent ownership is prevented |
| BDD-043 · Production upload uses the configured Pixel-compatible environment<br> | Given production remote storage mode is enabled<br>When a Google Photos upload begins<br>Then the configured Pixel-compatible upload environment is used<br>And a generic direct upload path is not substituted silently |
| BDD-044 · Upload environment is unavailable<br> | Given the required upload environment cannot start<br>When upload execution runs<br>Then no remote-success state is recorded<br>And the local file is preserved |
| BDD-045 · Authentication challenge requires operator action<br> | Given the Google account presents a login or verification challenge<br>When the adapter detects the challenge<br>Then execution becomes USER_ACTION_REQUIRED<br>And automatic credential submission stops |
| BDD-046 · Upload creates a remote asset<br> | Given a valid upload account<br>And a verified local media artifact<br>When upload completes<br>Then a RemoteAsset is recorded<br>And its provider is google_photos<br>And its durable reference is persisted |
| BDD-047 · UI success alone is not durable success<br>@critical | Given the upload UI indicates success<br>But no independent remote read-back has succeeded<br>When storage state is evaluated<br>Then the RemoteAsset is not DURABLE<br>And local deletion is forbidden |
| BDD-048 · Duplicate execution reconciles existing upload<br> | Given the same logical upload may already exist remotely<br>When a retried execution starts<br>Then pixAV first reconciles the remote operation<br>And avoids a duplicate upload when the existing asset can be proven |
| BDD-049 · Quota is counted exactly once<br>@critical | Given a remote upload succeeded<br>When upload success is committed<br>Then uploaded bytes are charged once<br>And retrying the commit does not charge them again |
| BDD-050 · Failed upload is not charged as successful usage<br> | Given remote creation did not succeed<br>When execution fails<br>Then successful-upload quota is not applied |
| BDD-051 · Quota reset uses database time<br> | Given an account is waiting for quota reset<br>When PostgreSQL time reaches the configured reset<br>Then the account may become eligible again |
| BDD-052 · New session can reacquire remote media<br>@critical | Given Google Photos upload completed<br>When pixAV creates a fresh independent retrieval session<br>Then the remote media can be located again |
| BDD-053 · Remote media metadata is consistent<br> | Given a remote asset is retrieved<br>When media probing runs<br>Then duration matches within tolerance<br>And codec expectations match<br>And expected media size constraints are satisfied |
| BDD-054 · Full read-back validates content<br>@critical | Given full verification is required<br>When the remote asset is read back completely<br>Then byte count matches the expected artifact<br>And content hash matches the expected artifact |
| BDD-055 · Verification failure invalidates remote durability<br> | Given a remote asset exists<br>When read-back verification fails<br>Then the asset does not become DURABLE<br>And local staging remains protected |
| BDD-056 · Verified remote asset becomes durable<br> | Given remote creation succeeded<br>And independent read-back succeeded<br>And configured integrity checks succeeded<br>When durability is committed<br>Then RemoteAsset becomes DURABLE |
| BDD-057 · Durable state survives temporary playback URL expiry<br> | Given a RemoteAsset is DURABLE<br>And a cached Google playback URL expires<br>When the video is requested again<br>Then durability remains valid<br>And only the ephemeral URL is refreshed |
| BDD-058 · Durable remote copy permits deletion candidate<br>@critical | Given the RemoteAsset is DURABLE<br>And playback projection is complete<br>And no active execution needs the file<br>And path safety checks pass<br>When cleanup evaluates the local staging artifact<br>Then it may become eligible for deletion |
| BDD-059 · Upload success without verification cannot delete<br>@critical | Given Google Photos reports successful upload<br>But RemoteAsset is not DURABLE<br>When cleanup runs<br>Then the local file is retained |
| BDD-060 · Active reader blocks deletion<br>@critical | Given the local file is currently serving an active playback or verification operation<br>When cleanup runs<br>Then deletion is rejected |
| BDD-061 · Unsafe path blocks deletion<br> | Given the deletion target is outside the configured staging root<br>When cleanup evaluates it<br>Then deletion is rejected |
| BDD-062 · Symlink escape blocks deletion<br> | Given the candidate path resolves outside the permitted root through a symlink<br>When cleanup evaluates it<br>Then deletion is rejected |
| BDD-063 · Successful deletion is audited<br> | Given a file is eligible for cleanup<br>When deletion succeeds<br>Then the result is recorded<br>And the domain no longer considers the local file authoritative |

### 6. Component D — PlaybackResolver

| ID／情境 | Given／When／Then 契約 |
| --- | --- |
| BDD-064 · Video has stable playback URL<br>@critical | Given a video is playback-ready<br>When its library projection is generated<br>Then the URL uses stable pixAV video identity<br>And does not embed an expiring Google Photos CDN URL |
| BDD-065 · Temporary provider URL expires<br> | Given Jellyfin requests the same stable pixAV URL<br>And the cached provider URL has expired<br>When PlaybackResolver handles the request<br>Then a fresh provider URL is resolved<br>And Jellyfin does not require a new library item |
| BDD-066 · Durable asset can become playback-ready<br> | Given a verified durable RemoteAsset exists<br>And playback preparation succeeds<br>When eligibility is evaluated<br>Then a PlayableAsset becomes READY |
| BDD-067 · Unverified remote asset cannot play<br> | Given RemoteAsset is not DURABLE<br>When a stream request arrives<br>Then playback is rejected<br>And unverified remote media is not exposed as complete content |
| BDD-068 · Full GET returns complete media<br> | Given a PlayableAsset is READY<br>When a client performs a full GET<br>Then the response succeeds<br>And the complete media bytes are returned |
| BDD-069 · Full GET matches verified artifact<br>@critical | Given expected byte count and SHA-256 are known<br>When a full verification GET completes<br>Then returned byte count matches<br>And SHA-256 matches |
| BDD-070 · HEAD returns media size<br> | Given a PlayableAsset is READY<br>When a client sends HEAD<br>Then no media body is returned<br>And Content-Length represents the playable object where known |
| BDD-071 · HEAD does not trigger full transfer<br> | Given a remote media is large<br>When HEAD is requested<br>Then pixAV does not read the complete remote object unnecessarily |
| BDD-072 · Initial range<br> | Given a playback-ready media asset<br>When the client requests "bytes=0-65535"<br>Then pixAV returns 206<br>And Content-Range is correct<br>And exactly the requested range is returned |
| BDD-073 · Middle range<br> | Given a playback-ready media asset<br>When the client requests bytes from the middle of the media<br>Then pixAV returns 206<br>And does not require transfer of all preceding bytes |
| BDD-074 · Suffix range<br> | Given a playback-ready media asset<br>When the client requests "bytes=-65536"<br>Then pixAV returns the final 65536 bytes<br>And returns 206 |
| BDD-075 · Out of range<br> | Given a media object has a known size<br>When the client requests a starting offset beyond the end<br>Then pixAV returns 416 |
| BDD-076 · Jellyfin seek does not require full replay from byte zero<br>@critical | Given playback has started<br>When Jellyfin seeks near the middle of the video<br>Then PlaybackResolver services an appropriate range request<br>And previously skipped media does not need to be downloaded in full |
| BDD-077 · Repeated nearby seek can use cache<br> | Given a temporary playback cache contains relevant bytes<br>When another request overlaps cached data<br>Then cached data may be reused |
| BDD-078 · Cold playback may materialize temporary content<br> | Given no local permanent media exists<br>And the remote asset is durable<br>When playback requires local materialization<br>Then a temporary cache artifact may be created |
| BDD-079 · Cache is not domain truth<br> | Given a temporary playback cache exists<br>When the cache is deleted<br>Then the durable RemoteAsset remains authoritative |
| BDD-080 · Cache expiry waits for active readers<br> | Given a cached artifact exceeded its TTL<br>But an active stream still reads it<br>When cache cleanup runs<br>Then the file is not deleted until the reader releases it |
| BDD-081 · Cache can be regenerated<br> | Given playback cache is missing<br>When the same video is requested later<br>Then PlaybackResolver can recreate required playback data from the durable remote asset |
| BDD-082 · First segment is not the complete movie<br>@critical | Given only the first segment has been prepared<br>When the stable stream endpoint is requested<br>Then pixAV returns an incomplete-media response<br>And does not expose segment one as the full video |
| BDD-083 · Completed segmented preparation becomes playable<br> | Given all required segments exist<br>And their junctions have been validated<br>When playback preparation finishes<br>Then the resulting PlayableAsset becomes READY |
| BDD-084 · Missing segment invalidates playback<br> | Given one required segment cannot be recovered<br>When playback preparation runs<br>Then PlayableAsset does not become READY |

### 7. Component E — LibraryProjection

| ID／情境 | Given／When／Then 契約 |
| --- | --- |
| BDD-085 · Only playback-ready media is published<br>@critical | Given a Video exists<br>And its PlayableAsset is READY<br>When publication reconciliation runs<br>Then the video is eligible for publication |
| BDD-086 · Durable storage alone is insufficient<br>@critical | Given a RemoteAsset is DURABLE<br>But PlayableAsset is not READY<br>When publication runs<br>Then no active Jellyfin projection is exposed |
| BDD-087 · Published item contains STRM<br> | Given a playback-ready video<br>When LibraryProjection exports it<br>Then a STRM file exists<br>And the STRM contains the stable pixAV playback URL |
| BDD-088 · STRM never contains temporary provider URL<br>@critical | Given Google Photos returned an expiring CDN URL<br>When LibraryProjection creates a STRM file<br>Then the CDN URL is not written<br>And the stable pixAV resolver URL is written instead |
| BDD-089 · Re-export is idempotent<br> | Given the STRM already contains the correct stable URL<br>When projection runs again<br>Then no duplicate library item is created |
| BDD-090 · Basic metadata is exported<br> | Given effective title and metadata exist<br>When NFO export runs<br>Then title is represented<br>And description is represented when known<br>And release metadata is represented when known |
| BDD-091 · Tags are exported<br> | Given a video has tags<br>When NFO export runs<br>Then every effective tag is represented in the Jellyfin-compatible metadata |
| BDD-092 · Performers are exported when known<br> | Given effective performer metadata exists<br>When NFO export runs<br>Then performer metadata is included |
| BDD-093 · Studio is exported when known<br> | Given effective studio metadata exists<br>When NFO export runs<br>Then studio information is included |
| BDD-094 · Missing optional metadata does not block publication<br> | Given a video has no known studio or performer<br>But playback is ready<br>When NFO export runs<br>Then publication succeeds with available metadata |
| BDD-095 · Manual override beats scraper value<br>@critical | Given a scraper supplied title A<br>And the user explicitly overrode title with B<br>When effective metadata is calculated<br>Then B is exported |
| BDD-096 · Scraper refresh does not erase manual override<br> | Given a field has a manual override<br>When external metadata is refreshed<br>Then the override remains effective |
| BDD-097 · Provider provenance is retained<br> | Given metadata is imported from an external provider<br>When it is stored<br>Then provider provenance remains available |
| BDD-098 · Primary poster is exported<br> | Given a valid local poster asset exists<br>When projection runs<br>Then Jellyfin-compatible poster artwork is present |
| BDD-099 · Backdrop is optional<br> | Given no backdrop exists<br>When publication runs<br>Then the item may still be published |
| BDD-100 · Remote artwork outage does not remove retained artwork<br> | Given poster artwork was previously cached locally<br>And the external metadata provider becomes unavailable<br>When projection is rebuilt<br>Then the retained poster remains usable |
| BDD-101 · Half-written item is not exposed<br>@critical | Given projection contains STRM, NFO and artwork<br>When export fails before all mandatory files are prepared<br>Then the final published directory is not atomically activated |
| BDD-102 · New projection replaces previous projection atomically<br> | Given an existing valid projection exists<br>And new metadata has been prepared<br>When publication succeeds<br>Then the complete new projection replaces the old one<br>And readers do not observe partially updated files |
| BDD-103 · Projection can be recreated from domain state<br>@critical | Given the entire generated Jellyfin projection has been deleted<br>And PostgreSQL domain data remains intact<br>When reconciliation runs<br>Then every eligible playback-ready item is regenerated |
| BDD-104 · Missing projection is recreated<br> | Given PostgreSQL marks an item eligible<br>But its Jellyfin projection is absent<br>When reconciliation runs<br>Then the projection is recreated |
| BDD-105 · Stale metadata projection is refreshed<br> | Given an item is already published<br>And effective metadata changed<br>When reconciliation runs<br>Then the generated metadata is updated |
| BDD-106 · Jellyfin never becomes source of truth<br> | Given a user deletes the generated Jellyfin projection manually<br>When reconciliation runs<br>Then pixAV reconstructs it from PostgreSQL |
| BDD-107 · Invalid playable asset is removed from active library<br> | Given a previously published PlayableAsset becomes INVALID<br>When projection reconciliation runs<br>Then the active Jellyfin projection is removed or disabled |
| BDD-108 · Projection removal does not delete durable media<br> | Given a Jellyfin projection is removed<br>When unpublication completes<br>Then the Google Photos RemoteAsset remains untouched |

### 8. Cross-Component Integration BDD

| ID／情境 | Given／When／Then 契約 |
| --- | --- |
| BDD-109 · Source becomes a private Jellyfin item<br>@e2e | Given an eligible source candidate exists<br>And an eligible Google Photos upload account exists<br>When pixAV processes the video end to end<br>Then the source is downloaded<br>And the resulting media is inspected<br>And required remuxing completes<br>And the media is uploaded through the configured upload environment<br>And Google Photos remote read-back succeeds<br>And the RemoteAsset becomes DURABLE<br>And the PlayableAsset becomes READY<br>And local staging becomes cleanup-eligible<br>And a Jellyfin STRM projection is created<br>And the stable pixAV stream can be played |
| BDD-110 · Local staging is removed only after complete verification<br>@e2e @destructive | Given a media file was successfully uploaded<br>And remote read-back verified its integrity<br>And playback verification passed<br>And library publication completed<br>And no active execution or reader uses the local file<br>When retention cleanup executes<br>Then the local staging file is removed<br>And the RemoteAsset remains durable<br>And Jellyfin playback remains available through pixAV |
| BDD-111 · Video remains playable after local source is removed<br>@e2e @critical | Given the local staging artifact has been deleted<br>And a durable Google Photos RemoteAsset exists<br>When Jellyfin starts playback<br>Then it requests the stable pixAV STRM URL<br>And PlaybackResolver reacquires the remote media<br>And playback starts successfully |
| BDD-112 · Seeking works after local removal<br>@e2e | Given no permanent local media exists<br>And playback is sourced from Google Photos<br>When Jellyfin seeks into the middle of the video<br>Then pixAV services the required range<br>And playback resumes near the requested position |

### 9. Failure Injection BDD

| ID／情境 | Given／When／Then 契約 |
| --- | --- |
| BDD-113 · Crash after remote upload before database commit<br>@chaos @critical | Given Google Photos upload already succeeded externally<br>But pixAV crashes before committing RemoteAsset success<br>When execution resumes<br>Then the existing remote upload is reconciled<br>And the same media is not uploaded a second time<br>And quota is charged at most once |
| BDD-114 · Crash after remote verification before local deletion<br>@chaos @critical | Given remote verification succeeded<br>But pixAV crashes before local cleanup<br>When recovery runs<br>Then the durable remote fact is retained<br>And cleanup eligibility is recomputed safely |
| BDD-115 · Crash during local deletion<br>@chaos | Given a local artifact is cleanup-eligible<br>When the cleanup process crashes<br>Then recovery verifies filesystem state<br>And domain state converges with the actual file state |
| BDD-116 · Redis is lost<br>@chaos | Given PostgreSQL retains all domain facts<br>When Redis state is cleared<br>Then no durable Google Photos asset is forgotten<br>And library projection remains rebuildable<br>And execution recovery follows the active execution system contract |
| BDD-117 · Jellyfin database is lost<br>@chaos | Given pixAV PostgreSQL and generated source metadata remain available<br>When Jellyfin is rebuilt<br>Then LibraryProjection can reconstruct eligible library items |
| BDD-118 · Temporary Google URL expires during playback<br>@chaos | Given playback is active<br>And a provider URL becomes invalid<br>When the resolver detects expiry<br>Then it reacquires a valid source when possible<br>And the stable Jellyfin STRM URL does not change |

### 10. Security / Privacy BDD

| ID／情境 | Given／When／Then 契約 |
| --- | --- |
| BDD-119 · Storage implementation details are hidden from Jellyfin<br>@security | Given Jellyfin requests a movie<br>When playback begins<br>Then Jellyfin receives the pixAV playback endpoint<br>And Google Photos account credentials are not exposed<br>And provider-internal identifiers are not unnecessarily exposed |
| BDD-120 · Secrets are never included in generated projection<br>@security | Given LibraryProjection exports STRM and NFO files<br>When the files are inspected<br>Then account passwords are absent<br>And cookies are absent<br>And Google authentication secrets are absent |
| BDD-121 · Unknown external client cannot access private playback<br>@security | Given deployment access controls are enabled<br>When an unauthorized client attempts to access playback<br>Then access is denied |
| BDD-122 · Revoked device loses access<br>@security | Given a previously authorized device is revoked<br>When it attempts new playback<br>Then access is denied |

### 11. Observability BDD

| ID／情境 | Given／When／Then 契約 |
| --- | --- |
| BDD-123 · Source exhaustion is observable<br> | Given all source candidates are unavailable<br>When workflow pauses for source recovery<br>Then a source-unavailable metric or event is emitted |
| BDD-124 · Upload waiting for quota is observable<br> | Given all accounts are quota-blocked<br>When an upload waits<br>Then WAITING_QUOTA is observable<br>And it is not reported as generic failure |
| BDD-125 · Remote verification failure is observable<br> | Given upload succeeded<br>But cold read-back verification failed<br>When the workflow stops<br>Then remote-verification failure is distinguishable from upload failure |
| BDD-126 · Publication lag is observable<br> | Given a video became playback-ready<br>But Jellyfin publication has not completed<br>When monitoring evaluates state<br>Then publication lag can be measured |

### 12. Manual Recovery BDD

| ID／情境 | Given／When／Then 契約 |
| --- | --- |
| BDD-127 · Failed execution can be inspected<br> | Given an execution terminated<br>When an operator inspects it<br>Then the final failure classification is available<br>And relevant domain identifiers are available<br>And secrets are not exposed |
| BDD-128 · Manual replay is explicit<br> | Given a terminally failed execution exists<br>When an operator requests replay<br>Then a new audited execution is created<br>And the old execution history is preserved |
| BDD-129 · DLQ is not automatically replayed forever<br> | Given an execution exhausted its automatic retry policy<br>When no operator action occurs<br>Then it remains terminal<br>And no unbounded replay loop occurs |

### 13. Cleanup Safety BDD

| ID／情境 | Given／When／Then 契約 |
| --- | --- |
| BDD-130 · Cleanup requires durable remote copy<br>@destructive @critical | Given no durable RemoteAsset exists<br>When cleanup evaluates a local media file<br>Then deletion is rejected |
| BDD-131 · Cleanup requires complete playback projection<br>@destructive @critical | Given RemoteAsset is DURABLE<br>But PlayableAsset is not READY<br>When cleanup evaluates the local media<br>Then deletion is rejected |
| BDD-132 · Cleanup requires no active task<br>@destructive | Given an open execution references the media<br>When cleanup runs<br>Then deletion is rejected |
| BDD-133 · Cleanup dry-run changes nothing<br>@destructive | Given cleanup identifies eligible files<br>When cleanup runs without apply mode<br>Then no filesystem changes occur |
| BDD-134 · Cleanup apply deletes exact approved target<br>@destructive | Given one exact path was approved<br>When cleanup runs in apply mode<br>Then only the approved target is removed |

### 14. Jellyfin Acceptance Matrix

| ID／情境 | Given／When／Then 契約 |
| --- | --- |
| BDD-135 · Library scan discovers STRM item<br>@jellyfin | Given a valid projection exists<br>When Jellyfin scans the library<br>Then exactly one corresponding item appears |
| BDD-136 · Poster is displayed<br>@jellyfin | Given projection contains poster artwork<br>When the Jellyfin item page opens<br>Then the poster is available |
| BDD-137 · Tags are searchable or filterable<br>@jellyfin | Given NFO contains video tags<br>When Jellyfin imports the item<br>Then the tags are available to library navigation |
| BDD-138 · Direct playback through pixAV<br>@jellyfin | Given the client can consume the resolved media<br>When playback starts<br>Then Jellyfin can play the pixAV STRM source |
| BDD-139 · Seek works<br>@jellyfin | Given playback has started<br>When the user seeks to the middle<br>Then playback resumes without restarting from byte zero |
| BDD-140 · Resume state remains Jellyfin responsibility<br>@jellyfin | Given a user stops halfway<br>When the same item is reopened<br>Then Jellyfin may resume using its own playback history<br>And pixAV does not duplicate Jellyfin watch-state logic |

### 15. Production Promotion Gate

| ID／情境 | Given／When／Then 契約 |
| --- | --- |
| BDD-141 · Component is eligible for production<br>@promotion | Given contract tests pass<br>And isolated integration tests pass<br>And failure recovery tests pass<br>And rollback has been tested<br>And no previous executor can concurrently mutate the same execution state<br>When production promotion is requested<br>Then the component may be promoted |
| BDD-142 · Failed acceptance blocks promotion<br>@promotion | Given any critical BDD scenario is failing<br>When production promotion is requested<br>Then promotion is rejected |

### 18. 最重要的 Golden Path

| ID／情境 | Given／When／Then 契約 |
| --- | --- |
| BDD-143 · Media survives local deletion and remains playable<br>@production_canary @critical | Given one verified source candidate exists<br>And one eligible Google Photos account exists<br>When pixAV downloads the selected source<br>And FFmpeg prepares the media if required<br>And media inspection succeeds<br>And the media is uploaded through the configured Pixel-compatible environment<br>And Google Photos reports the remote item<br>And pixAV performs a fresh cold read-back<br>And full integrity verification succeeds<br>And the RemoteAsset becomes DURABLE<br>And the PlayableAsset becomes READY<br>And Jellyfin projection is published<br>And Jellyfin successfully plays and seeks the video<br>And local retention cleanup executes<br>Then the staging media file is removed<br>When Jellyfin plays the same video again<br>Then pixAV resolves the durable Google Photos asset<br>And playback succeeds without the original local file<br>And seeking succeeds<br>And no duplicate Google Photos upload is created<br>And account quota was charged exactly once |
