# 更新文件

1. 遵循根目錄 `AGENTS.md`，讀相關本地規則；核對 worktree、callers、schema/migration、Compose、測試及可核實 runtime evidence，保留既有修改。
2. 以五個核心邊界與 remote-only Golden Path 更新 `README.md`；用途、目標、實作差距、開發順序、操作與完整 BDD 都集中於此。規則放 `AGENTS.md`，`CLAUDE.md` 僅作入口。
3. 保留 README 的 BDD ID、情境與驗收語意。新規格整合到既有文件，不另建 roadmap、CODEMAPS、ADR、archive、流水帳或轉址頁；文件直接刪改，不建立備份。
4. 明確區分規範、程式實作、隔離證據與 production 驗收。上傳成功、share URL、local finalize、AVAILABLE 或 coverage 不證明 DURABLE／READY；未核實保持未驗收。
5. 使用中性用語與合成媒體；保留實際路徑、參數、資料安全、完整 cleanup gate 與 fallback 退休條件。不要記錄私有來源或 credential。
6. 修正引用，檢查相對連結、shell 命令語法、BDD 完整性及變更範圍。純文件變更不啟動外部副作用；回報實際完成的檢查與未驗證事項。
