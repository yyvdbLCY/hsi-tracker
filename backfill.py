import json
import requests
import time
import datetime
import os
import sys
import firebase_admin
from firebase_admin import credentials, firestore

# ---------- 設定 ----------
# 你可以透過命令列傳入日期，例如：python backfill.py 2026-06-29
if len(sys.argv) > 1:
    date_str = sys.argv[1]
    try:
        target_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        print("日期格式錯誤，請用 YYYY-MM-DD")
        exit(1)
else:
    # 如果冇傳參數，預設補尋日
    target_date = datetime.date.today() - datetime.timedelta(days=1)

print(f"📅 補捉日期：{target_date.isoformat()}")

# ---------- Firebase 初始化 ----------
# 確保 serviceAccountKey.json 存在（同你主爬蟲一樣）
cred = credentials.Certificate("serviceAccountKey.json")
firebase_admin.initialize_app(cred)
db = firestore.client()
doc_ref = db.collection("market").document("hsi_data")

# ---------- 複用你主爬蟲嘅函數 ----------
# 直接從 cbbc_crawler.py import（確保兩個檔案喺同一目錄）
# 或者你可以直接複製下面三個函數過嚟（避免 import 問題）
# 呢度我用 import 方式，如果你嘅 cbbc_crawler.py 冇問題可以直接咁做
try:
    from cbbc_crawler import (
        get_hsi_last_from_sg,
        fetch_and_parse_hkex,
        fallback_sg_full,
        save_data,
        upload_to_firestore
    )
except ImportError:
    # 如果 import 失敗，直接複製函數內容（請手動複製你 cbbc_crawler.py 嗰幾個函數過嚟）
    print("⚠️ 無法從 cbbc_crawler 導入，請將以下函數複製到 backfill.py：")
    print("get_hsi_last_from_sg, fetch_and_parse_hkex, fallback_sg_full, save_data, upload_to_firestore")
    exit(1)

# ---------- 執行補捉 ----------
hsi = get_hsi_last_from_sg()
data, err = fetch_and_parse_hkex(target_date, hsi_last=hsi)
if data is None:
    print(f"⚠️ 港交所失敗：{err}，改用全法興回退")
    try:
        data = fallback_sg_full()
        data["date"] = target_date.isoformat()   # 確保日期正確
    except Exception as e:
        print(f"❌ 回退失敗：{e}")
        exit(1)

save_data(data, archive=True)
upload_to_firestore(data)
print(f"✅ 已補捉 {target_date.isoformat()} 數據並上傳 Firestore")
