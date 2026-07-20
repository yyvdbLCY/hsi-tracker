"""
Google Sheets 完整備份腳本
=========================
把 CBBC + Options OI 每日資料寫到 Google Sheets,作為永久備份。

Sheets 結構 (一個 spreadsheet,4 個 tab):
  1. CBBC_Summary       - 每日 CBBC 摘要 (date | hsi | bull_pct | ...)
  2. CBBC_Distribution  - 每日完整分布 (date | type | strike | low | high | volume)
  3. OptionsOI_Summary  - 每日 Options 摘要 (date | update_time | last_close | call_pct | put_pct | ...)
  4. OptionsOI_Strikes  - 每日完整 strikes (date | strike | call_oi | call_oi_change | call_pct | ...)

認證: 透過 service account (JSON key) 從環境變數讀取。
環境變數:
  GOOGLE_SHEETS_CREDENTIALS - service account JSON 內容
  GOOGLE_SHEET_ID           - spreadsheet ID (URL /d/ 後面那段)

行為: 每次執行都重讀本地檔案 + archive/,做完整覆寫
       確保 sheet 跟本地檔案永遠一致 (idempotent)。
"""
import json
import os
import sys
from pathlib import Path

# ================= 配置 =================
OUTPUT_FILE_CBBC = "cbbc_distribution.json"
OUTPUT_FILE_OI = "options_oi_distribution.json"
ARCHIVE_DIR = "archive"

TAB_CBBC_SUMMARY = "CBBC_Summary"
TAB_CBBC_DIST = "CBBC_Distribution"
TAB_OI_SUMMARY = "OptionsOI_Summary"
TAB_OI_STRIKES = "OptionsOI_Strikes"
# ========================================


def get_credentials_dict():
    """從環境變數讀 service account JSON"""
    creds_str = os.environ.get("GOOGLE_SHEETS_CREDENTIALS", "")
    if not creds_str:
        return None
    try:
        return json.loads(creds_str)
    except json.JSONDecodeError as e:
        print(f"❌ GOOGLE_SHEETS_CREDENTIALS 不是有效 JSON: {e}")
        return None


def get_sheet_id():
    return os.environ.get("GOOGLE_SHEET_ID", "").strip()


def load_all_data():
    """讀本地主檔 + 所有 archive,合併去重"""
    data = {"cbbc": [], "options_oi": []}

    # CBBC
    main_path = Path(OUTPUT_FILE_CBBC)
    if main_path.exists():
        try:
            with main_path.open() as f:
                data["cbbc"].append(json.load(f))
        except Exception as e:
            print(f"⚠️ 讀 {OUTPUT_FILE_CBBC} 失敗: {e}")

    # Options OI
    main_path = Path(OUTPUT_FILE_OI)
    if main_path.exists():
        try:
            with main_path.open() as f:
                data["options_oi"].append(json.load(f))
        except Exception as e:
            print(f"⚠️ 讀 {OUTPUT_FILE_OI} 失敗: {e}")

    # Archive
    archive_path = Path(ARCHIVE_DIR)
    if archive_path.exists():
        for f in sorted(archive_path.glob("cbbc_*.json")):
            try:
                with f.open() as fp:
                    rec = json.load(fp)
                if rec not in data["cbbc"]:
                    data["cbbc"].append(rec)
            except Exception as e:
                print(f"⚠️ 讀 {f} 失敗: {e}")
        for f in sorted(archive_path.glob("options_oi_*.json")):
            try:
                with f.open() as fp:
                    rec = json.load(fp)
                if rec not in data["options_oi"]:
                    data["options_oi"].append(rec)
            except Exception as e:
                print(f"⚠️ 讀 {f} 失敗: {e}")

    # 按日期排序
    data["cbbc"].sort(key=lambda x: x.get("date", ""))
    data["options_oi"].sort(key=lambda x: x.get("date", ""))
    return data


def cbbc_to_summary_rows(records):
    """CBBC Summary tab: 1 row per day"""
    headers = ["date", "hsi", "bull_pct", "bull_amount", "bear_amount", "bull_500"]
    rows = [headers]
    for r in records:
        s = r.get("summary", {})
        rows.append([
            r.get("date", ""),
            r.get("hsi", ""),
            s.get("bull_pct", ""),
            s.get("total_bull", ""),
            s.get("total_bear", ""),
            s.get("bull_500", ""),
        ])
    return rows


def cbbc_to_distribution_rows(records):
    """CBBC Distribution tab: 1 row per (date, strike)"""
    headers = ["date", "type", "strike", "low", "high", "volume"]
    rows = [headers]
    for r in records:
        for d in r.get("distribution", []):
            rows.append([
                r.get("date", ""),
                d.get("type", ""),
                d.get("strike", ""),
                d.get("low", ""),
                d.get("high", ""),
                d.get("volume", ""),
            ])
    return rows


def oi_to_summary_rows(records):
    """OptionsOI Summary tab: 1 row per day"""
    headers = ["date", "update_time", "last_close", "call_pct", "put_pct", "strike_count"]
    rows = [headers]
    for r in records:
        rows.append([
            r.get("date", ""),
            r.get("update_time", ""),
            r.get("last_close", ""),
            r.get("call_pct", ""),
            r.get("put_pct", ""),
            r.get("strike_count", ""),
        ])
    return rows


def oi_to_strikes_rows(records):
    """OptionsOI Strikes tab: 1 row per (date, strike)"""
    headers = [
        "date", "strike",
        "call_oi", "call_oi_change", "call_pct", "call_flags",
        "put_oi", "put_oi_change", "put_pct", "put_flags",
    ]
    rows = [headers]
    for r in records:
        for s in r.get("strikes", []):
            rows.append([
                r.get("date", ""),
                s.get("strike", ""),
                s.get("call_oi", ""),
                s.get("call_oi_change", ""),
                s.get("call_pct", ""),
                ",".join(s.get("call_flags", [])),
                s.get("put_oi", ""),
                s.get("put_oi_change", ""),
                s.get("put_pct", ""),
                ",".join(s.get("put_flags", [])),
            ])
    return rows


def ensure_tabs(sh, tab_names):
    """確保所有需要的 tab 存在,缺的自動建"""
    existing = {ws.title for ws in sh.worksheets()}
    for name in tab_names:
        if name not in existing:
            print(f"   + 建立新 tab: {name}")
            sh.add_worksheet(title=name, rows=100, cols=20)


def write_tab(sh, tab_name, rows):
    """整批覆寫 tab 內容"""
    ws = sh.worksheet(tab_name)
    ws.clear()
    if rows:
        ws.update(rows, "A1")
    # 驗證: 讀回來確認
    if rows:
        verify = ws.get_all_values()
        if len(verify) != len(rows):
            raise RuntimeError(f"{tab_name} 驗證失敗: 寫了 {len(rows)} 列但讀回 {len(verify)} 列")
        print(f"      ✓ {tab_name}: 寫入 + 讀回驗證 OK ({len(rows)} rows)")
    else:
        print(f"      ⚠ {tab_name}: 0 rows 跳過")
    return len(rows)


def main():
    creds = get_credentials_dict()
    sheet_id = get_sheet_id()

    if not creds:
        print("ℹ️  GOOGLE_SHEETS_CREDENTIALS 未設定,跳過 Sheets 備份")
        print("   設定方法見 README 或對話記錄")
        return 0  # 視為成功,不要讓 workflow fail
    if not sheet_id:
        print("ℹ️  GOOGLE_SHEET_ID 未設定,跳過 Sheets 備份")
        return 0

    try:
        import gspread
    except ImportError:
        print("❌ gspread 未安裝,請 pip install gspread")
        return 1

    print(f"📊 開始備份到 Google Sheets (sheet ID: {sheet_id[:10]}...)")

    # 認證 + 開啟 sheet
    try:
        gc = gspread.service_account_from_dict(creds)
        sh = gc.open_by_key(sheet_id)
    except Exception as e:
        print(f"❌ 開啟 Sheet 失敗: {e}")
        print("   確認 sheet ID 正確 + service account email 已被加入為編輯者")
        return 1

    # 確保所有 tab 存在
    print("🔧 確保 tabs 存在...")
    ensure_tabs(sh, [TAB_CBBC_SUMMARY, TAB_CBBC_DIST, TAB_OI_SUMMARY, TAB_OI_STRIKES])

    # 載入所有本地資料
    data = load_all_data()
    print(f"📂 載入本地資料: CBBC {len(data['cbbc'])} 天, Options OI {len(data['options_oi'])} 天")

    # 寫入
    n = 0
    n += write_tab(sh, TAB_CBBC_SUMMARY, cbbc_to_summary_rows(data["cbbc"]))
    n += write_tab(sh, TAB_CBBC_DIST, cbbc_to_distribution_rows(data["cbbc"]))
    n += write_tab(sh, TAB_OI_SUMMARY, oi_to_summary_rows(data["options_oi"]))
    n += write_tab(sh, TAB_OI_STRIKES, oi_to_strikes_rows(data["options_oi"]))

    print(f"✅ Sheets 備份完成,共寫入 {n} rows")
    print(f"   🔗 https://docs.google.com/spreadsheets/d/{sheet_id}/edit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
