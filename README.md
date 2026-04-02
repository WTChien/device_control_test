# device_control_test

使用 Python + ADB 自動化操作聊天機器人 App，並擷取機器人回覆。即使畫面文字無法直接選取或複製，也能透過本工具取得結果。

## 專案目的

本專案主要用於測試國泰 beta 版本聊天流程與回覆擷取能力。
目前先以 ChatGPT App 作為測試對象，用來驗證整體自動化流程可行性，後續可替換為國泰 beta App 進行正式測試。

## 工具功能

- 透過 ADB 點擊與輸入事件，將提示詞送到 Android 聊天機器人 App。
- 每次送出提示詞後自動擷取螢幕。
- 兩階段擷取回覆文字：
  - 第一階段：使用 UIAutomator XML dump 擷取文字，並比對送出前後畫面，只保留新出現的內容。
  - 第二階段：若第一階段失敗，改用截圖區域 OCR。
- 儲存每個測試案例結果與總結紀錄，方便後續驗證。

## 檔案說明

- `chatbot_test_runner.py`：主要自動化腳本。
- `requirements.txt`：Python 相依套件。

## 前置需求

1. 已安裝 ADB，且可在 PATH 中使用。
2. Android 裝置已連線並授權（`adb devices` 顯示 `device`）。
3. Python 3.10 以上版本。
4. 已安裝 Tesseract OCR 引擎（供 OCR 備援使用）。

在 macOS 上：

```bash
brew install tesseract
```

若需要繁體中文 OCR：

```bash
brew install tesseract-lang
```

## 安裝 Python 相依套件

```bash
pip install -r requirements.txt
```

## 取得座標

你需要設定三組座標：

- 輸入框點位：`x,y`
- 送出按鈕點位：`x,y`
- 機器人回覆區域：`left,top,right,bottom`

快速取得方式：

1. 在手機上打開 App。
2. 執行 `adb shell wm size` 查看螢幕解析度。
3. 執行 `adb exec-out screencap -p > screen.png`，再用圖片工具查看像素座標。

## 執行單一提示詞

```bash
python chatbot_test_runner.py \
  --package com.your.app \
  --activity .MainActivity \
  --prompt "你好，請自我介紹" \
  --input-point 120,2230 \
  --send-point 1010,2230 \
  --response-region 40,300,1040,2050 \
  --session-name chatgpt_test \
  --wait-sec 6
```

## 執行多筆提示詞

先建立 `prompts.txt`（每行一筆提示詞），再執行：

```bash
python chatbot_test_runner.py \
  --package com.your.app \
  --prompt-file prompts.txt \
  --input-point 120,2230 \
  --send-point 1010,2230 \
  --response-region 40,300,1040,2050 \
  --session-name chatgpt_batch
```

## ChatGPT App 範例

使用內建預設值的最快方式：

```bash
python chatbot_test_runner.py
```

等同於：

```bash
python chatbot_test_runner.py --chatgpt-auto-once
```

如果預設座標不符合你的裝置 UI，請改用明確座標：

若要測試 Android ChatGPT App，可先嘗試：

```bash
python chatbot_test_runner.py \
  --package com.openai.chatgpt \
  --prompt "請用一句話介紹今天的天氣" \
  --input-point 120,2230 \
  --send-point 1010,2230 \
  --response-region 40,300,1040,2050 \
  --session-name chatgpt_live \
  --wait-sec 8
```

## Cube beta App 範例

若目標 App 是 Cube beta，可先用目前畫面直接驗證 Android UI 是否可抓到文字：

```bash
python chatbot_test_runner.py --cube-beta-auto-once --capture-current-ui
```

若要做一輪自動送訊息測試：

```bash
python chatbot_test_runner.py --cube-beta-auto-once --prompt "hello"
```

Cube beta 預設會：

- 使用套件 `com.cathaybk.pokemon.mew`
- 嘗試自動偵測輸入框位置
- 嘗試自動推算回覆區域
- 優先找送出按鈕，找不到時退回 Enter 送出

## 輸出結果

- `outputs/summary.jsonl`：每個測試案例一筆 JSON 紀錄。
- `outputs/<session-name>_transcript.txt`：可讀性較高的逐字稿，包含 USER 與 BOT 內容。
- `outputs/case_XXX/screen.png`：每個案例的截圖。
- `outputs/case_XXX/result.json`：擷取出的回覆與相關中繼資料。

## 聊天機器人測試注意事項

- 若 App 文字可在 Accessibility tree 中取得，通常會用 `ui_dump` 擷取（品質較好）。
- 若 App 剛啟動時畫面上已有歡迎詞、建議按鈕或其他靜態文案，腳本會先記錄送出前內容，再從送出後結果中扣除，避免把首頁文案誤判成新回覆。
- 若看不到（常見於自繪聊天 UI），腳本會自動改用 OCR。
- 若網路或模型回覆較慢，請提高 `--wait-sec`。
- 縮小 `--response-region` 可以降低 OCR 雜訊。
- 多數裝置的原生 `adb shell input text` 僅支援 ASCII。
- 若要送出中文或其他 Unicode 提示詞，請在裝置安裝 ADB Keyboard。若系統偵測到，腳本會暫時切換並自動輸入文字。
- 若自動偵測不到，可加上 `--adb-keyboard-ime` 指定 IME ID，例如：`--adb-keyboard-ime com.android.adbkeyboard/.AdbIME`。
