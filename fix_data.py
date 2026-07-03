import firebase_admin
from firebase_admin import credentials, firestore

# 初始化
cred = credentials.Certificate("serviceAccountKey.json")
firebase_admin.initialize_app(cred)
db = firestore.client()
doc_ref = db.collection("market").document("hsi_data")

# 正確數據（來自法興官網）
correct_data = [
       {
        "date": "2026-07-02",
        "hsi": 23026.68,          # 你用返法興顯示嗰個恒指，或者用 Yahoo 查返當日收市
        "bull": 51.2,
        "bull_amount": 7120,
        "bear_amount": 6778,
        "bull_500_amount": 1750      # 你稍後可以補返正確嘅500點內數字
    },
    {
        "date": "2026-06-29",
        "hsi": 23026.68,          # 你用返法興顯示嗰個恒指，或者用 Yahoo 查返當日收市
        "bull": 51.0,
        "bull_amount": 6612,
        "bear_amount": 6341,
        "bull_500_amount": 1160      # 你稍後可以補返正確嘅500點內數字
    },
    {
        "date": "2026-06-30",
        "hsi": 22881.02,          # 根據你 log 法興俾嘅現價
        "bull": 53.3,
        "bull_amount": 7181,
        "bear_amount": 6297,
        "bull_500_amount": 2606.5
    }
]

# 更新 Firestore
doc = doc_ref.get()
data_list = doc.get("list") if doc.exists else []

for correct in correct_data:
    updated = False
    for i, item in enumerate(data_list):
        if item["date"] == correct["date"]:
            data_list[i] = correct
            updated = True
            break
    if not updated:
        data_list.append(correct)
        data_list.sort(key=lambda x: x["date"])

doc_ref.set({"list": data_list})
print(f"✅ 已修正 {len(correct_data)} 筆數據")
