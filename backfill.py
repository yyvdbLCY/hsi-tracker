import json
import requests
import time
import datetime
import os
import sys
import firebase_admin
from firebase_admin import credentials, firestore

# ---------- 設定 ----------
if len(sys.argv) > 1:
    date_str = sys.argv[1]
    try:
        target_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        print("日期格式錯誤，請用 YYYY-MM-DD")
        exit(1)
else:
    target_date = datetime.date.today() - datetime.timedelta(days=1)

print(f"📅 補捉日期：{target_date.isoformat()}")

cred = credentials.Certificate("serviceAccountKey.json")
firebase_admin.initialize_app(cred)
db = firestore.client()
doc_ref = db.collection("market").document("hsi_data")

try:
    from cbbc_crawler import (
        get_hsi_last_from_sg,
        fetch_and_parse_hkex,
        fallback_sg_full,
        save_data,
        upload_to_firestore
    )
except ImportError:
    print("⚠️ 無法從 cbbc_crawler 導入，請將以下函數複製到 backfill.py")
    exit(1)

hsi = get_hsi_last_from_sg()
data, err = fetch_and_parse_hkex(target_date, hsi_last=hsi)
if data is None:
    print(f"⚠️ 港交所失敗：{err}，改用全法興回退 (target_date={target_date.isoformat()})")
    try:
        # 法興 API 只保留 ~3 週資料,較舊的日期會拿到空資料
        data = fallback_sg_full(target_date=target_date)
        data["date"] = target_date.isoformat()   # 確保日期正確
    except Exception as e:
        print(f"❌ 回退失敗：{e}")
        exit(1)

save_data(data, archive=True)
upload_to_firestore(data)
print(f"✅ 已補捉 {target_date.isoformat()} 數據並上傳 Firestore")
