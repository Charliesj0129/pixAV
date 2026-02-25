# pixAV Development Roadmap

本文件基於專案目前的架構與設定檔 (如 `README.md` 與 `pyproject.toml`) 生成，概述了目前的技術棧、模組規劃以及未來的發展里程碑。

## 🛠 技術棧 (Tech Stack)

### 後端基礎設施 (Backend Infrastructure)

- **語言**: Python 3.10+ (目前主力), Rust (未來重構計畫)
- **資料庫**: PostgreSQL (`asyncpg`, `pgvector`)
- **唯讀快取與佇列**: Redis (`redis.asyncio`)
- **API 伺服器**: FastAPI + Uvicorn (應用於 `strm_resolver`)

### 核心處理與依賴 (Core Processing & Dependencies)

- **網頁爬蟲與解析**: `httpx`, `beautifulsoup4`, `lxml` (`sht_probe` 模組)
- **媒體處理**: `ffmpeg-python` (`media_loader` 模組)
- **自動化控制**: `uiautomator2` (`pixel_injector` 模組搭配 Redroid)
- **序列化與設定管理**: `pydantic`, `pydantic-settings`
- **容器化與調度**: Docker, Docker Compose (`docker` SDK)

### 開發與維護工具 (DevOps & QA)

- **測試**: Pytest, `pytest-asyncio`, `testcontainers`
- **程式碼品質**: Ruff, Black, isort, Mypy
- **部署**: Docker Compose

---

## 🏗 模組架構與現狀 (Architecture & Current State)

系統目前分為五大模組，以 Redis Queue 串接，並以 PostgreSQL 作為資料的唯一真相來源 (Source of Truth)：

1. **`sht_probe` (爬蟲與磁力連結探索)**
   - **狀態**: 已有基礎實作與 Cloudflare 繞過整合 (如 Sehuatang)。
   - **功能**: 定期掃描目標論壇，抓取並解析種子檔案或磁力連結。

2. **`media_loader` (下載與封裝處理)**
   - **狀態**: 雛形實作 (Stub/Skeleton)。
   - **功能**: 操作 BT 客戶端 (如 qBittorrent) 進行下載，並透過 FFmpeg 將影片重新封裝、壓縮或轉檔，準備給予注入階段使用。

3. **`pixel_injector` (行動裝置注入與雲端上傳)**
   - **狀態**: 最小可行性產品 (MVP)，已具備自動化登入能力。
   - **功能**: 動態開啟 Redroid (Android 14) 容器，透過 ADB 與 UIAutomator2 模擬真人登入 Google 帳號，並驅動 Google Photos 自動備份流程。

4. **`maxwell_core` (調度與背壓控制)**
   - **狀態**: 雛形實作 (Stub)。
   - **功能**: 負責派發任務給 Redis Queue、執行資源配額管理 (Quotas)、監控機制與垃圾回收 (GC)。

5. **`strm_resolver` (串流解析與代理)**
   - **狀態**: 雛形架構 (Skeleton)。
   - **功能**: 提供 FastAPI 端點供外部播放器提取串流真實位址，將 Google Photos 分享連結即時轉譯為直接播放連結。

---

## 🚀 開發里程碑 (Milestones)

### Phase 1: 最小可行性串接 (MVP E2E) - _Current_

- [x] 設計資料庫 Schema (Migrations)。
- [x] 完成 `sht_probe` 對核心論壇的爬取與 Cloudflare 繞過。
- [x] 實作 `pixel_injector` 的 Android 模擬器建立與自動化 Google 登入。
- [ ] 完善 `media_loader` 與 qBittorrent 的掛載與自動下載。
- [ ] 串聯全管線：從爬蟲發現影片到順利產生 Google Photos 分享網址。

### Phase 2: 穩定化與核心調度優化 (Stabilization)

- [ ] 在 `maxwell_core` 中實作完整的背壓 (Backpressure) 與佇列深度監控。
- [ ] 強化錯誤處理、Retries 機制與通知警報 (Alerting)。
- [ ] 建構 `strm_resolver` 使前端播放器可直接消耗爬取並上傳好的影片。
- [ ] 整備 `docker-compose.prod.yml` 進入正式生產環境部署。

### Phase 3: 效能與架構演進 (High Performance & Rust Rewrite)

- [ ] 確立並穩定 `src/pixav/shared/` 內的所有共用資源合約與 Models。
- [ ] 分析效能瓶頸，將高負載模組 (如 `maxwell_core` 或 `strm_resolver`) 依序遷移至 `rust/` 工作區進行重構。
- [ ] 橫向擴展：支援多路 Redis 分配與多台節點部署 `pixel_injector` 以應付大量上傳。
