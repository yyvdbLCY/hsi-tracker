import requests
import json
import datetime
import time
import os
import sys  # 引入 sys 用於控制退出狀態
import firebase_admin
from firebase_admin import credentials, firestore

# ========== 法興 API 設定 ==========
API_URL = "https://hk.warrants.com/hk/data/chart/stock_cbbc_real2.cgi"

def fetch_market_data():
    today_str = datetime.date.today().isoformat()
    params = {
        "ucode": "HSI",
        "spread": "100",
        "sdate": "",
        "_": int(time.time() * 1000)
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://hk.warrants.com/tc/cbbc/outstanding-distribution",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest"
    }
    try:
        resp = requests.get(API_URL, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"❌ API 請求失敗: {e}")
        return {"status": "error", "reason": str(e)}

    fd = data.get("furtherData", {})
    sum_bull = int(fd.get("sumBull", 0))
    sum_bear = int(fd.get("sumBear", 0))
    hsi_last = float(fd.get("hsilast", 0))
    data_date = fd.get("sdate", today_str)

    if sum_bull + sum_bear == 0:
        print("ℹ️ 今日為非交易日，跳過")
        return {"status": "holiday"}

    bull_pct = round(sum_bull / (sum_bull + sum_bear) * 100, 1)

    # 500 點內牛證張數
    bull_500_sum = 0
    chart_data = data.get("mainData", [])
    for item in chart_data:
        if item.get('ty') != 'bull':
            continue
        o1_val = item.get('o1')
        if o1_val is None:
            continue
        try:
            bull_vol = int(round(float(o1_val)))
        except (ValueError, TypeError):
            continue
        if bull_vol == 0:
            continue
        p_min = item.get('fr')
        p_max = item.get('to')
        if p_min is None or p_max is None:
            continue
        if p_max >= (hsi_last - 500) and p_min <= hsi_last:
            bull_500_sum += bull_vol

    CORRECTION_FACTOR = 1.713
    bull_500_sum = int(round(bull_500_sum * CORRECTION_FACTOR))

    return {
        "status": "success",
        "data": {
            "date": data_date,
            "hsi": hsi_last,
            "bull": bull_pct,
            "bull_amount": sum_bull,
            "bear_amount": sum_bear,
            "bull_500_amount": bull_500_sum
        }
    }

# ========== Firebase 初始化 ==========
try:
    # 讀取環境變數並修正可能存在的換行符號問題
    firebase_key_raw = os.environ.get("FIREBASE_KEY", "{}")
    # strict=False 可以防止一些隱藏的轉義字元報錯
    firebase_key_dict = json.loads(firebase_key_raw, strict=False)
    
    cred = credentials.Certificate(firebase_key_dict)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    doc_ref = db.collection("market").document("hsi_data")
except Exception as e:
    print(f"❌ Firebase 初始化失敗，請檢查 FIREBASE_KEY 的 JSON 內容: {e}")
    sys.exit(1)

def upload_to_firestore(new_data):
    doc = doc_ref.get()
    data_list = doc.get("list") if doc.exists else []
    updated = False
    for i, item in enumerate(data_list):
        if item.get("date") == new_data["date"]:
            data_list[i] = new_data
            updated = True
            break
    if not updated:
        data_list.append(new_data)
        data_list.sort(key=lambda x: x["date"])
    doc_ref.set({"list": data_list})
    print(f"✅ Firestore 已更新，共 {len(data_list)} 筆記錄")

if __name__ == "__main__":
    result = fetch_market_data()
    
    if result["status"] == "success":
        upload_to_firestore(result["data"])
    elif result["status"] == "holiday":
        # 假期跳過，屬於正常現象，以 code 0 正常結束
        sys.exit(0)
    else:
        # 真正的抓取失敗，以 code 1 結束，GitHub Actions 會亮紅叉 ❌ 提醒你
        print(f"⚠️ 任務失敗原因: {result['reason']}")
        sys.exit(1)
