# 開發入口

先遵循 [AGENTS.md](AGENTS.md)；目標架構、實作差距、開發順序、操作守衛與完整 BDD 見 [README.md](README.md)。以「安全刪除 staging 並清空播放 cache 後，仍可從 Google Photos 恢復播放與 seek」作為完成標準。

Windows 開發採 [Windows Android Emulator + Docker Runner](README.md#開發環境windows-android-emulator--docker-runner)，使用 Microsoft WSL kernel。`bzImage-6.6.87.2-pixav-redroid` 的全域 kernel override 已有操作者回報的 Docker／WSL 相容性問題，不再作為開發方案。[Android runner Compose](docker-compose.android-runner.yml) 是獨立工具入口；既有 managed 上傳仍是 redroid 實作、待遷移，不可把 runner health 或 AVD 的 Pixel 名稱當作上傳／quota／durability 驗收。

新 cohort 的實播須有可重算的版本化影音證據與操作者實看實聽確認；自動 capture、服務健康或尚未滿 24 小時的觀察都不能宣告完成。

本次階段 3–5 採操作者授權的本機加密完整備份／隔離完整還原例外；離機與 production 關卡仍 OPEN。登入原帳號的 cold 回載須使用獨立新 session、僅認證資料及共用下載 adapter；登入成功不等於原件或 durability 驗證成功。操作契約與停止條件見 README。

工作區的可選登入模式使用 `PIXAV_PHOTOS_COLD_AUTH_SECRET` 與 `photos-cold-account-v1` 收據；storage／playback 共用下載、完整解碼與帳號綁定守衛。完整軟體檢查、映像部署、live 原件回載與人工實播各自驗收，不能由程式存在推定通過。

容器只啟動當前階段必要的服務，重用已核對的部署；臨時驗證逐一執行、完成即移除。以 exact container ID 清除錯誤或已被取代的部署，保留資料 volumes、staging 與證據，不使用 prune。

階段 3／4 的原 operation 覆核、補充錄影、容量狀態及主機恢復後的 identity 核對與驗收界線見 README 的 2026-10-08 更新；軟體檢查、隔離 live、人工確認與連續 24 小時驗收分開記錄。未知遠端結果不重傳，缺完整備份還原收據不替換資料。

手機確認的既有項目可作 v2 candidate 覆核輸入；受控端須核對 exact item、先記分享 intent，再通過獨立完整回載。舊觀察不刷新時間，人工回報不升級為 DURABLE。

2026-10-09 新隔離 session 再次核對既有分享位置的目標片段；Google 原件仍在處理、暫不可下載。此為 provider 等待關卡，尚未 cold 驗證或 apply 覆核，不能重傳或提前記帳。

前兩片已完成操作者影音與 reference 對齊確認、完整錄影重算、當前 DB binding 及 HTTP／Jellyfin 檢查，正式登錄 `jellyfin-client-v2`。主機恢復後的空掛載已修復，原資料及映像保留；本機 client gateway 改用 `127.0.0.1:18096`。第三片原件處理與有效 24 小時容量證據仍未完成，不宣告整輪通過。

驗證保留完整 coverage 報告，不以全 src 80% 作門檻；CI 執行行為測試及核對實例身分的 PostgreSQL／Redis 整合測試。BDD 與 live／production 關卡仍依 README，不能以 CI 通過取代。

本輪清理的現況核對以 README 為準：停機前全部實際 bind 已核對一致，包含 Jellyfin；私有媒體清理仍缺完整備份／還原收據。歷史修復與驗收不代替新一輪恢復證據。

2026-10-10 已授權 Docker 搬移與精確釋放舊 C 副本；服務已停寫，完整 DB／roles／ledger／queue sidecar 保存，原資料保留。2026-10-11 接續搬移指示已授權本次全 WSL 釋鎖；15 個原容器未於一致 checkpoint 後啟動、28 個 host bind 已核對，背景作業 hash／語法／dry-run 通過。完整備份、還原及搬移仍依各自成功收據判定，尚未宣稱通過。專案已精簡無 caller 依賴與介面、核對後清除可重建產物；軟體與實體容量驗證各自記錄於 README。
