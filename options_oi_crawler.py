"""
HSI 即月認購/認沽期權 OI 牆位爬蟲
===================================
目標: https://hk.warrants.com/tc/options/open-interest
資料流向:
  1. Playwright 抓取 + JS 渲染
  2. 解析表格 → strikes[] (call OI / put OI / flags)
  3. 解析 metadata (update_time / call_pct / put_pct / last_close)
  4. 寫入 options_oi_distribution.json (latest)
  5. 寫入 archive/options_oi_YYYY-MM-DD.json (歷史)
  6. 上傳到 Firebase Firestore: market/hsi_options_oi
"""
import json
import os
import re
import sys
import time
import datetime
import firebase_admin
from firebase_admin import credentials, firestore

# ================= 配置 =================
TARGET_URL = "https://hk.warrants.com/tc/options/open-interest"
OUTPUT_FILE = "options_oi_distribution.json"
ARCHIVE_DIR = "archive"

# GitHub Actions runner 上的 Playwright Chromium 路徑
# 本地 sandbox 也是同一條路徑 (ms-playwright cache)
CHROME_CANDIDATES = [
    "/root/.cache/ms-playwright/chromium-1223/chrome-linux/chrome",
    "/root/.cache/ms-playwright/chromium-1187/chrome-linux/chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
]
# ========================================


def find_chrome():
    for path in CHROME_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def get_browser():
    """啟動 Playwright Chromium,自動處理 sandbox 參數"""
    from playwright.sync_api import sync_playwright
    chrome_path = find_chrome()
    pw = sync_playwright().start()
    kwargs = dict(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
        ],
    )
    if chrome_path:
        kwargs["executable_path"] = chrome_path
    b = pw.chromium.launch(**kwargs)
    return pw, b


def parse_oi(text):
    """ '1,266[+2] 重貨區' → (1266, 2, ['重貨區']) """
    flags = []
    if "重貨區" in text:
        flags.append("重貨區")
    if "最多新增" in text:
        flags.append("最多新增")
    m = re.match(r"\s*([\d,]+)\s*\[([+\-]?\d+)\]", text.strip())
    if m:
        return int(m.group(1).replace(",", "")), int(m.group(2)), flags
    return None, None, flags


def scrape():
    """主爬取邏輯"""
    pw, b = get_browser()
    try:
        page = b.new_page(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)
        # 等 JS 渲染完 (hk.warrants.com 比較慢,8-10 秒穩妥)
        page.wait_for_timeout(10000)

        # 一次拿全部資料,只走一次 IPC
        raw = page.evaluate("""() => {
            const tables = document.querySelectorAll('table');
            if (tables.length < 1) return { error: 'no tables found' };
            const t0 = tables[0];
            const rows = t0.querySelectorAll('tr');
            const data = [];
            for (let i = 1; i < rows.length; i++) {  // skip header
                const cells = rows[i].querySelectorAll('td');
                if (cells.length >= 7) {
                    data.push(Array.from(cells).map(c => c.innerText.replace(/\\s+/g, ' ').trim()));
                }
            }
            const body = document.body.innerText;
            return {
                data,
                update_time: (body.match(/最後更新時間\\s*[:：]?\\s*([0-9\\-:\\s]+)/) || [])[1] || '',
                call_pct:    parseFloat((body.match(/即月認購期權街貨\\s*([0-9.]+)%/) || [])[1] || 0),
                put_pct:     parseFloat((body.match(/即月認沽期權街貨\\s*([0-9.]+)%/) || [])[1] || 0),
                last_close:  (body.match(/上日收市價\\s*([0-9,.]+)/) || [])[1] || '',
            };
        }""")

        if "error" in raw:
            raise RuntimeError(raw["error"])

        if not raw.get("data"):
            raise RuntimeError("no data rows scraped")

        strikes = []
        for cells in raw["data"]:
            # Layout: [Call%][empty][Call OI [chg] flags][Strike][Put OI [chg]][empty][Put%]
            call_pct = re.sub(r"[^\d.]", "", cells[0]) or "0"
            call_oi, call_oi_chg, call_flags = parse_oi(cells[2])
            try:
                strike = int(cells[3].replace(",", ""))
            except (ValueError, IndexError):
                continue
            put_oi, put_oi_chg, put_flags = parse_oi(cells[4])
            put_pct = re.sub(r"[^\d.]", "", cells[6]) or "0"

            strikes.append({
                "strike": strike,
                "call_pct": float(call_pct),
                "call_oi": call_oi,
                "call_oi_change": call_oi_chg,
                "call_flags": call_flags,
                "put_oi": put_oi,
                "put_oi_change": put_oi_chg,
                "put_pct": float(put_pct),
                "put_flags": put_flags,
            })

        # 清理 update_time 尾端換行
        update_time = raw.get("update_time", "").strip()

        # 用「上日收市價的日期」當作 data date (最合理)
        # 抓不到就用今天
        if update_time:
            # 格式 "2026-07-20 07:00:00" → "2026-07-20"
            data_date = update_time.split()[0]
        else:
            data_date = datetime.date.today().isoformat()

        try:
            last_close = float(raw.get("last_close", "").replace(",", ""))
        except (ValueError, TypeError):
            last_close = 0.0

        result = {
            "date": data_date,
            "update_time": update_time,
            "last_close": last_close,
            "call_pct": raw.get("call_pct", 0),
            "put_pct": raw.get("put_pct", 0),
            "strike_count": len(strikes),
            "strikes": strikes,
        }
        return result
    finally:
        b.close()
        pw.stop()


def upload_to_firestore(data):
    """上傳到 Firestore: market/hsi_options_oi"""
    if not firebase_admin._apps:
        cred = credentials.Certificate("serviceAccountKey.json")
        firebase_admin.initialize_app(cred)
    db = firestore.client()
    doc_ref = db.collection("market").document("hsi_options_oi")

    try:
        doc = doc_ref.get()
        data_list = doc.get("list") if doc.exists else []

        updated = False
        for i, item in enumerate(data_list):
            if item.get("date") == data["date"]:
                data_list[i] = data
                updated = True
                break
        if not updated:
            data_list.append(data)
            data_list.sort(key=lambda x: x.get("date", ""))

        # 只保留最近 90 天
        if len(data_list) > 90:
            data_list = data_list[-90:]

        doc_ref.set({"list": data_list})
        print(f"✅ Firestore 已更新,共 {len(data_list)} 筆歷史記錄")
    except Exception as e:
        print(f"❌ Firestore 上傳失敗: {e}")
        raise


def save_data(data):
    """寫本地 JSON: 主文件 + archive"""
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    archive_path = os.path.join(ARCHIVE_DIR, f"options_oi_{data['date']}.json")
    with open(archive_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main():
    today = datetime.date.today()
    print(f"📅 開始抓取 {today.isoformat()} 的 HSI 期權 OI 牆位...")

    try:
        data = scrape()
    except Exception as e:
        print(f"❌ 爬取失敗: {e}")
        sys.exit(1)

    save_data(data)
    upload_to_firestore(data)

    s = data
    print(f"✅ 抓取完成:")
    print(f"   資料日期:    {s['date']}")
    print(f"   更新時間:    {s['update_time']}")
    print(f"   上日收市:    {s['last_close']:,.2f}")
    print(f"   認購街貨:    {s['call_pct']:.1f}%")
    print(f"   認沽街貨:    {s['put_pct']:.1f}%")
    print(f"   行使價檔數:  {s['strike_count']}")
    print(f"📁 已存檔: {OUTPUT_FILE} + archive/options_oi_{s['date']}.json")


if __name__ == "__main__":
    main()
