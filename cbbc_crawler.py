import json
import requests
import time
import datetime
import os
import pandas as pd
import zipfile
import io
import firebase_admin
from firebase_admin import credentials, firestore

# ================= 配置 =================
OUTPUT_FILE = "cbbc_distribution.json"
ARCHIVE_DIR = "archive"                 # 历史备份文件夹
CORRECTION_FACTOR = 1.0                 # 港交所数据已为全市场官方统计，不再需要校正
# ========================================

# ---------- Firebase 初始化 ----------
cred = credentials.Certificate("serviceAccountKey.json")
firebase_admin.initialize_app(cred)
db = firestore.client()
doc_ref = db.collection("market").document("hsi_data")

def upload_to_firestore(data):
    """将爬虫结果转换为前端格式并合并到 Firestore"""
    # 从前端需要的字段构建记录
    s = data["summary"]
    record = {
        "date": data["date"],
        "hsi": data["hsi"],
        "bull": s["bull_pct"],                     # 牛证比例
        "bull_amount": s["total_bull"],            # 牛证张数
        "bear_amount": s["total_bear"],            # 熊证张数
        "bull_500_amount": s["bull_500"]           # 500点内重货牛证
    }

    try:
        doc = doc_ref.get()
        data_list = doc.get("list") if doc.exists else []

        # 如果已有相同日期，则更新；否则新增
        updated = False
        for i, item in enumerate(data_list):
            if item.get("date") == record["date"]:
                data_list[i] = record
                updated = True
                break
        if not updated:
            data_list.append(record)
            # 按日期排序（确保倒序显示时正确）
            data_list.sort(key=lambda x: x["date"])

        doc_ref.set({"list": data_list})
        print(f"✅ Firestore 已更新，共 {len(data_list)} 笔记录")
    except Exception as e:
        print(f"❌ Firestore 上传失败: {e}")

# ---------- 以下为你原有代码（未修改） ----------
def get_hsi_last_from_sg():
    """仅从法兴 API 获取恒指现价"""
    url = "https://hk.warrants.com/hk/data/chart/stock_cbbc_real2.cgi"
    params = {
        "ucode": "HSI",
        "spread": "100",
        "sdate": "",
        "_": int(time.time() * 1000)
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://hk.warrants.com/tc/cbbc/outstanding-distribution",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        hsi = float(data.get("furtherData", {}).get("hsilast", 0))
        return hsi
    except Exception:
        return None

def fetch_and_parse_hkex(target_date: datetime.date, hsi_last: float = None):
    # ... 你的原有代码，保持不变 ...
    # 为了节省篇幅，这里省略，实际请保留你原来的完整函数
    pass

def fallback_sg_full():
    # ... 你的原有代码，保持不变 ...
    pass

def save_data(data, archive=True):
    # ... 你的原有代码，保持不变 ...
    pass

def main():
    today = datetime.date.today()
    print(f"📅 开始获取 {today.isoformat()} 的牛熊证数据（主渠道：港交所）...")

    # 先尝试获取法兴现价（仅用于填补港交所无现价的问题）
    hsi_from_sg = get_hsi_last_from_sg()
    if hsi_from_sg:
        print(f"📈 从法兴获取恒指现价：{hsi_from_sg}")
    else:
        print("⚠️ 法兴现价获取失败，将使用估算值")

    # 主渠道：港交所
    data, err = fetch_and_parse_hkex(today, hsi_last=hsi_from_sg)
    source = "港交所（主）"

    if data is None:
        print(f"⚠️ 港交所数据获取失败：{err}")
        print("🔄 完全回退到法兴 API...")
        try:
            data = fallback_sg_full()
            source = "法兴（完全回退）"
        except Exception as e2:
            print(f"❌ 法兴回退也失败：{e2}")
            return

    save_data(data, archive=True)
    
    # 🔥 新增：上传到 Firestore
    upload_to_firestore(data)

    s = data["summary"]
    print(f"✅ 数据来源：{source}")
    print(f"📊 总牛证: {s['total_bull']:,} | 总熊证: {s['total_bear']:,}")
    print(f"🎯 500点内重货牛证: {s['bull_500']:,}")
    print(f"📈 恒指现价: {data['hsi']}")
    print(f"📁 分布档位数: {len(data['distribution'])}")
    print(f"📦 历史备份已保存至 {ARCHIVE_DIR}/cbbc_{data['date']}.json")

if __name__ == "__main__":
    main()
