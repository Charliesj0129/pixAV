# pixAV Pipeline Optimization TODOs

目的：把五大模塊與共享契約的優化項目整理成可逐項開工的清單，優先聚焦中間三個模塊：`media_loader`、`pixel_injector`、`maxwell_core`。

## 開工順序（建議）

1. `P0` 共享契約與文件對齊（避免越改越漂）
2. `P1` `maxwell_core` 狀態語義 / backpressure / lease
3. `P1` `media_loader` 清理與輸出目錄隔離
4. `P1` `pixel_injector` 鎖與 ADB 連線模型
5. `P2` `sht_probe` 批次化與 Protocol 對齊
6. `P2` `strm_resolver` singleflight / CORS 收斂
7. `P3` 觀測性與覆蓋率補強（全模塊）

## P0 共享契約 / 架構文件（先做）

- [ ] `TODO-CONTRACT-01` 對齊 `Task` model 與 `tasks` schema 欄位（至少釐清 `trace_id`, `local_path`, `share_url` 是否應持久化）
  - 參考：`src/pixav/shared/models.py:62`
  - 參考：`migrations/001_initial_schema.sql:101`
  - 參考：`src/pixav/shared/repository.py:311`

- [ ] `TODO-CONTRACT-02` 定義統一 queue payload schema（typed DTO / Pydantic model），取代各 worker 手動 parse `dict`
  - 參考：`src/pixav/maxwell_core/dispatcher.py:46`
  - 參考：`src/pixav/media_loader/worker.py:89`
  - 參考：`src/pixav/pixel_injector/worker.py:34`

- [ ] `TODO-CONTRACT-03` 決定 `pixav:verify` 的去留（保留並實作 / 移除文件與殘留狀態）
  - 參考：`README.md:60`
  - 參考：`docs/CODEMAPS/data-flow.md:12`
  - 參考：`src/pixav/config.py:91`

- [ ] `TODO-DOC-01` 修正 `docs/CODEMAPS/data-flow.md` 的實際 producer/consumer 與模塊順序敘述
  - 參考：`docs/CODEMAPS/data-flow.md:5`
  - 參考：`src/pixav/maxwell_core/worker.py:127`
  - 參考：`src/pixav/maxwell_core/worker.py:169`

## P1 中間三模塊（重點）

### `maxwell_core`（調度中樞）

- [ ] `TODO-MC-01` 修正 task state 語義：避免「已 dispatch 但尚未被 worker 實際執行」就標成 transient state
  - 目標：降低 orphan cleanup 誤判
  - 參考：`src/pixav/maxwell_core/orchestrator.py:77`
  - 參考：`src/pixav/maxwell_core/gc.py:18`

- [ ] `TODO-MC-02` Backpressure 監控納入 `:processing` inflight 深度（不只看主 queue）
  - 參考：`src/pixav/maxwell_core/backpressure.py:51`
  - 參考：`src/pixav/shared/queue.py:26`

- [ ] `TODO-MC-03` 重新設計帳號 lease 生命周期（dispatch 後 `mark_used` 太早）
  - 目標：把 lease 完成/釋放與 upload 結果連動
  - 參考：`src/pixav/maxwell_core/orchestrator.py:128`
  - 參考：`src/pixav/maxwell_core/scheduler.py:85`
  - 參考：`src/pixav/pixel_injector/worker.py:460`

- [ ] `TODO-MC-04` `ingest_crawl_queue()` 去重策略升級（DB 唯一約束 + insert 衝突處理），避免 race 時重複 task
  - 參考：`src/pixav/maxwell_core/worker.py:82`
  - 參考：`src/pixav/maxwell_core/worker.py:94`

- [ ] `TODO-MC-05` 將 `TaskScheduler` / `TaskDispatcher` Protocol 的字串介面升級為 typed contract（例如 `AccountLease`, `DispatchEnvelope`）
  - 參考：`src/pixav/maxwell_core/interfaces.py:12`
  - 參考：`src/pixav/maxwell_core/interfaces.py:25`

### `media_loader`（下載/轉檔）

- [ ] `TODO-ML-01` 確保失敗路徑也會清理 torrent（download 成功但 remux 失敗時不可殘留）
  - 參考：`src/pixav/media_loader/service.py:83`
  - 參考：`src/pixav/media_loader/service.py:95`

- [ ] `TODO-ML-02` 分離 `download_dir` 與 remux 輸出目錄，避免檔名碰撞與清理風險
  - 參考：`src/pixav/media_loader/worker.py:68`
  - 參考：`src/pixav/media_loader/service.py:80`
  - 參考：`src/pixav/media_loader/remuxer.py:85`

- [ ] `TODO-ML-03` `QBitClient` 改為持久 `httpx.AsyncClient` + session/cookie 管理（避免每次重新登入）
  - 參考：`src/pixav/media_loader/qbittorrent.py:69`
  - 參考：`src/pixav/media_loader/qbittorrent.py:101`
  - 參考：`src/pixav/media_loader/qbittorrent.py:127`
  - 參考：`src/pixav/media_loader/qbittorrent.py:173`

- [ ] `TODO-ML-04` 補充 download/remux 階段 metrics（成功/失敗/重試/耗時）
  - 參考：`src/pixav/shared/metrics.py:17`
  - 參考：`src/pixav/media_loader/worker.py:80`

- [ ] `TODO-ML-05` 明確化 `media_loader` queue payload 契約（目前 worker 仍容忍多種 payload 形狀）
  - 參考：`src/pixav/media_loader/worker.py:89`

### `pixel_injector`（上傳 / Redroid / ADB）

- [ ] `TODO-PI-01` 修正 upload 分散式鎖釋放為原子操作（Lua compare-and-del），避免 `GET`/`DEL` race
  - 參考：`src/pixav/pixel_injector/worker.py:140`

- [ ] `TODO-PI-02` 為 upload 鎖加入 TTL 續租（heartbeat），避免長任務鎖過期導致雙重處理
  - 參考：`src/pixav/pixel_injector/worker.py:360`
  - 參考：`src/pixav/pixel_injector/worker.py:416`
  - 參考：`src/pixav/pixel_injector/service.py:102`

- [ ] `TODO-PI-03` 釐清 `upload_max_concurrency` 語義並落實真正併發執行（目前只影響是否上鎖）
  - 參考：`src/pixav/pixel_injector/worker.py:344`
  - 參考：`src/pixav/pixel_injector/worker.py:564`

- [ ] `TODO-PI-04` 重構 `AdbConnection` 為無共享 mutable target（每 task/session 一個 adb client 或 target 顯式傳遞）
  - 參考：`src/pixav/pixel_injector/adb.py:22`
  - 參考：`src/pixav/pixel_injector/adb.py:34`
  - 參考：`src/pixav/pixel_injector/worker.py:535`

- [ ] `TODO-PI-05` `UIAutomatorUploader.login()` 從固定 sleep/keyevent 改為狀態感知流程（selector / screen assertions / step retry）
  - 參考：`src/pixav/pixel_injector/uploader.py:46`
  - 參考：`src/pixav/pixel_injector/uploader.py:50`
  - 參考：`src/pixav/pixel_injector/uploader.py:68`

- [ ] `TODO-PI-06` Redroid readiness 檢查升級（容器 running != ADB truly ready），明確 ADB readiness 與 Android boot 完成條件
  - 參考：`src/pixav/pixel_injector/redroid.py:123`
  - 參考：`src/pixav/pixel_injector/adb.py:40`
  - 參考：`src/pixav/pixel_injector/adb.py:43`

- [ ] `TODO-PI-07` 補齊 upload worker metrics（成功/失敗/重試/DLQ replay/鎖等待）
  - 參考：`src/pixav/shared/metrics.py:17`
  - 參考：`src/pixav/pixel_injector/worker.py:391`
  - 參考：`src/pixav/pixel_injector/worker.py:477`

- [ ] `TODO-PI-08` 重新檢視帳號不存在/失效時的錯誤分類（目前大多直接變 upload failure）
  - 參考：`src/pixav/pixel_injector/service.py:43`
  - 參考：`src/pixav/pixel_injector/worker.py:455`

## P2 其他兩個模塊

### `sht_probe`

- [ ] `TODO-SP-01` 對齊 `ContentCrawler` / `MagnetExtractor` Protocol 與實作簽名（目前介面/實作不一致）
  - 參考：`src/pixav/sht_probe/interfaces.py:12`
  - 參考：`src/pixav/sht_probe/interfaces.py:28`
  - 參考：`src/pixav/sht_probe/crawler.py:41`
  - 參考：`src/pixav/sht_probe/parser.py:20`

- [ ] `TODO-SP-02` `_persist_new()` 批次化（批次查重 + `INSERT ... ON CONFLICT` + Redis pipeline）
  - 參考：`src/pixav/sht_probe/service.py:179`
  - 參考：`src/pixav/sht_probe/service.py:215`
  - 參考：`src/pixav/sht_probe/service.py:217`

- [ ] `TODO-SP-03` 讓 `HttpxCrawler` 使用持久 `AsyncClient`（目前每次 fetch 建立新 client）
  - 參考：`src/pixav/sht_probe/crawler.py:73`

- [ ] `TODO-SP-04` 補 crawl/search 階段 metrics（抓取量、失敗率、新磁鏈數、跳過原因）
  - 參考：`src/pixav/sht_probe/service.py:87`
  - 參考：`src/pixav/sht_probe/service.py:226`

### `strm_resolver`

- [ ] `TODO-SR-01` 在 `_resolve_cdn()` 加入 per-`video_id` singleflight，避免 cache miss stampede
  - 參考：`src/pixav/strm_resolver/routes.py:69`
  - 參考：`src/pixav/strm_resolver/resolver.py:31`

- [ ] `TODO-SR-02` CORS 收斂（避免 `allow_origins=["*"]` + `allow_credentials=True`）
  - 參考：`src/pixav/strm_resolver/middleware.py:66`

- [ ] `TODO-SR-03` 釐清/修正文檔與註解：rate limit 實作是 fixed window bucket，不是 sliding window
  - 參考：`src/pixav/strm_resolver/middleware.py:17`
  - 參考：`src/pixav/strm_resolver/middleware.py:36`

- [ ] `TODO-SR-04` resolver 與 routes 補 metrics（cache hit/miss、resolve latency、error code）
  - 參考：`src/pixav/strm_resolver/routes.py:73`
  - 參考：`src/pixav/strm_resolver/routes.py:121`
  - 參考：`src/pixav/strm_resolver/resolver.py:49`

## P3 測試 / 觀測性 / 維運

- [ ] `TODO-TEST-01` 提升中間三模塊 worker 覆蓋率（特別是 `pixel_injector` / `maxwell_core`）
  - 參考：`docs/PROJECT_HEALTH_REVIEW_2026-02-16.md:57`

- [ ] `TODO-OBS-01` 實際接上 `shared.metrics`（目前有定義但未被各 worker 使用）
  - 參考：`src/pixav/shared/metrics.py:17`

- [ ] `TODO-OPS-01` 為關鍵 worker 增加一致的 structured logging 欄位（`task_id`, `video_id`, `trace_id`, `queue_name`, `account_id`）
  - 參考：`src/pixav/media_loader/worker.py:132`
  - 參考：`src/pixav/pixel_injector/worker.py:288`
  - 參考：`src/pixav/maxwell_core/orchestrator.py:135`

## 建議第一批開工（最穩妥）

- [ ] `TODO-CONTRACT-01`
- [ ] `TODO-CONTRACT-03`
- [ ] `TODO-MC-01`
- [ ] `TODO-MC-02`
- [ ] `TODO-ML-01`
- [ ] `TODO-PI-01`
- [ ] `TODO-PI-02`

