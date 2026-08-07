#!/usr/bin/env python3
"""
Backfill script: 從 archive/cbbc_YYYY-MM-DD.json 重新計算近價 ±1000 內
的 bull / bear 街貨總和, 補到:
  1. archive/cbbc_YYYY-MM-DD.json 的 summary
  2. Firestore `market/hsi_data` 的 list 內每筆 record
  3. (sheets_backup.py 會自然讀 archive, Sheets 也會跟著補)

跑法:
    python3 backfill_bull_1000.py                 # 預設 30 天
    python3 backfill_bull_1000.py --days 60        # 自訂天數
    python3 backfill_bull_1000.py --archive-only   # 只更新 archive 不連 Firestore
"""
import json
import os
import sys
import glob
import argparse
from datetime import datetime, timedelta

ARCHIVE_DIR = "archive"
FIRESTORE_DOC = ("market", "hsi_data")


def recompute_1000(distribution, hsi_last):
    """從 distribution 重算近價 ±1000 內的牛熊證張數總和"""
    if not distribution or hsi_last is None or hsi_last <= 0:
        return 0, 0
    bull_1000 = 0
    bear_1000 = 0
    for d in distribution:
        strike = d.get("strike", 0)
        volume = d.get("volume", 0)
        if d.get("type") == "bull" and (hsi_last - 1000) <= strike <= hsi_last:
            bull_1000 += volume
        elif d.get("type") == "bear" and hsi_last <= strike <= (hsi_last + 1000):
            bear_1000 += volume
    return int(bull_1000), int(bear_1000)


def backfill_archive(days=30):
    """更新 archive 內的 summary, 回傳 {(date): (bull_1000, bear_1000)}"""
    today = datetime.now().date()
    cutoff = today - timedelta(days=days)
    pattern = os.path.join(ARCHIVE_DIR, "cbbc_*.json")
    files = sorted(glob.glob(pattern))

    archive_updates = {}  # {date_str: (bull_1000, bear_1000)}
    updated = 0
    skipped = 0
    for fp in files:
        basename = os.path.basename(fp)
        try:
            date_str = basename.replace("cbbc_", "").replace(".json", "")
            file_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            print(f"  ⚠️  {basename}: 日期解析失敗, 跳過")
            skipped += 1
            continue

        if file_date < cutoff:
            continue

        with open(fp, "r", encoding="utf-8") as f:
            data = json.load(f)

        hsi = data.get("hsi")
        dist = data.get("distribution", [])
        summary = data.get("summary", {})

        # 已存在且非零, 跳過
        if summary.get("bull_1000", 0) > 0 and summary.get("bear_1000", 0) > 0:
            archive_updates[date_str] = (summary["bull_1000"], summary["bear_1000"])
            skipped += 1
            continue

        bull_1000, bear_1000 = recompute_1000(dist, hsi)
        summary["bull_1000"] = bull_1000
        summary["bear_1000"] = bear_1000
        data["summary"] = summary

        with open(fp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        archive_updates[date_str] = (bull_1000, bear_1000)
        print(f"  ✓ archive {date_str}: HSI={hsi}, bull_1000={bull_1000}, bear_1000={bear_1000}")
        updated += 1

    print(f"📦 archive: {updated} 個更新, {skipped} 個跳過")
    return archive_updates


def backfill_firestore(archive_updates):
    """把 archive 的 1000 數值套到 Firestore 的 list 內對應 record"""
    if not os.environ.get("FIREBASE_KEY") and not os.path.exists("serviceAccountKey.json"):
        print("⚠️  找不到 FIREBASE_KEY env 或 serviceAccountKey.json, 跳過 Firestore")
        return False

    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
    except ImportError:
        print("⚠️  firebase_admin 沒安裝, 跳過 Firestore (pip install firebase-admin)")
        return False

    if not firebase_admin._apps:
        if os.environ.get("FIREBASE_KEY"):
            cred = credentials.Certificate(json.loads(os.environ["FIREBASE_KEY"]))
        else:
            cred = credentials.Certificate("serviceAccountKey.json")
        firebase_admin.initialize_app(cred)

    db = firestore.client()
    coll, doc_id = FIRESTORE_DOC
    doc_ref = db.collection(coll).document(doc_id)
    doc = doc_ref.get()
    if not doc.exists:
        print(f"⚠️  Firestore {coll}/{doc_id} 不存在, 跳過")
        return False
    data_list = doc.get("list") or []
    print(f"📥 Firestore 現有 {len(data_list)} 筆 record")

    updated_count = 0
    new_list = []
    for item in data_list:
        date_str = item.get("date")
        if date_str in archive_updates:
            b1, b2 = archive_updates[date_str]
            item["bull_1000_amount"] = b1
            item["bear_1000_amount"] = b2
            updated_count += 1
        new_list.append(item)

    if updated_count > 0:
        doc_ref.set({"list": new_list})
        print(f"✅ Firestore 更新 {updated_count} 筆 record (補 bull_1000_amount + bear_1000_amount)")
    else:
        print(f"ℹ️  Firestore 沒有對應日期的 record 需要更新")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30, help="回填天數 (default 30)")
    ap.add_argument("--archive-only", action="store_true", help="只更新 archive 不連 Firestore")
    args = ap.parse_args()

    print(f"🔄 backfill 近價 ±1000 牛熊證張數 (回填 {args.days} 天)\n")

    updates = backfill_archive(days=args.days)

    if not args.archive_only:
        print()
        backfill_firestore(updates)

    print(f"\n💡 Sheets 會在下次 sheets_backup.py 執行時自動補 (讀 archive)")


if __name__ == "__main__":
    main()
