"""
HSI 即月認購/認沽期權 OI 牆位爬蟲 (XML API 版)
====================================================
目標 API: https://hk.warrants.com/hk/data/chart/cbbc_oichart.cgi
          ?ucode=HSI&sdate=YYYY-MM-DD&r_expiry=radioTM&step=N

step 參數: 每方向顯示 N 個 strike,實際 strike 數 = 2*N
  step=10  → 20 strikes  (±~7%)
  step=20  → 40 strikes  (±~15%)
  step=50  → 85 strikes  (±~28%, 推薦)
  step=80+ → 101 strikes (max, 16,000-32,000)

API 回傳 XML 結構:
  <level>
    <last>25143.05</last>     <!-- HSI 上日收市 -->
    <max>1243</max>           <!-- 最大 OI (圖表用) -->
    <c_oi>16029</c_oi>        <!-- 全口徑認購 OI -->
    <p_oi>8348</p_oi>         <!-- 全口徑認沽 OI -->
    <stime>2026-07-21 07:00:00</stime>
    <step>
      <type>up|down</type>     <!-- up=strike>last, down=strike<last -->
      <strike>27000</strike>
      <oi_call>817</oi_call>
      <oi_put>3</oi_put>
      <y_oi_call>801</y_oi_call>   <!-- 昨日 OI -->
      <y_oi_put>3</y_oi_put>
      <os_call>5.10%</os_call>     <!-- 街貨佔比 % -->
      <os_put>0.04%</os_put>
    </step>
    ...
  </level>

資料流向:
  1. requests 直接打 XML API (不再用 Playwright,快 3-5x)
  2. XML 解析 → strikes[] (call OI / put OI / flags / relative position)
  3. 寫入 options_oi_distribution.json (latest)
  4. 寫入 archive/options_oi_YYYY-MM-DD.json (歷史)
  5. 上傳到 Firebase Firestore: market/hsi_options_oi
"""
import json
import os
import re
import sys
import datetime
import time
import csv
import io
import requests

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    _HAS_FIREBASE = True
except ImportError:
    _HAS_FIREBASE = False

# ================= 配置 =================
# step=50 → 85 strikes (覆蓋 HSI ±28%),實務夠用且不過長
OI_STEP = 50

# 主源 (hk.warrants.com XML API)
API_BASE = "https://hk.warrants.com/hk/data/chart/cbbc_oichart.cgi"

# 後備源 (hkiei.com iframe → 7desl.com/hkex CSV)
# 160 strikes (16,000-32,000), T+0 (當日日結後上傳)
BACKUP_BASE = "https://7desl.com/data"

OUTPUT_FILE = "options_oi_distribution.json"
ARCHIVE_DIR = "archive"

# 近價判定門檻 (用於 is_near_money flag)
NEAR_MONEY_THRESHOLD = 500  # HSI ±500 為近價
# ========================================


def get_credentials_dict():
    return None  # 不需要,本檔用 serviceAccountKey.json


def fetch_oi_xml(target_date=None, step=OI_STEP):
    """打 XML API 拿 HSI 即月 OI 數據"""
    if target_date is None:
        target_date = datetime.date.today()
    sdate = target_date.strftime("%Y-%m-%d") if isinstance(target_date, datetime.date) else target_date

    url = f"{API_BASE}?ucode=HSI&sdate={sdate}&r_expiry=radioTM&step={step}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": "https://hk.warrants.com/tc/options/open-interest",
        "X-Requested-With": "XMLHttpRequest",
    }
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.text, url


def parse_oi_xml(xml_text, source_url=""):
    """
    解析 XML → dict
    回傳結構:
    {
      "last_close": 25143.05,
      "update_time": "2026-07-21 07:00:00",
      "total_call_oi": 16029,
      "total_put_oi": 8348,
      "max_oi": 1243,
      "step": 50,
      "strikes": [
        {
          "strike": 27000, "type": "up",
          "call_oi": 817, "put_oi": 3,
          "y_call_oi": 801, "y_put_oi": 3,
          "call_pct": 5.10, "put_pct": 0.04,
          "call_oi_change": 16, "put_oi_change": 0,
          "relative_position": 1857,    # strike - last_close
          "distance_pct": 7.39,        # 相對 HSI 距離 %
          "is_near_money": False,
          "call_flags": ["重貨區"],     # if os_call >= 5%
          "put_flags": [],
        }, ...
      ]
    }
    """
    # 抓 metadata
    def extract(tag, cast=str, default=None):
        m = re.search(rf"<{tag}>([^<]+)</{tag}>", xml_text)
        if not m: return default
        try: return cast(m.group(1))
        except: return default

    last_close = extract("last", float, 0.0)
    max_oi = extract("max", int, 0)
    total_call = extract("c_oi", int, 0)
    total_put = extract("p_oi", int, 0)
    update_time = extract("stime", str, "")

    # 抓所有 <step>...</step>
    step_blocks = re.findall(r"<step>\s*(.*?)\s*</step>", xml_text, re.DOTALL)

    strikes = []
    for block in step_blocks:
        def b(tag, cast=str, default=None):
            m = re.search(rf"<{tag}>([^<]+)</{tag}>", block)
            if not m: return default
            try: return cast(m.group(1))
            except: return default

        strike = b("strike", int)
        if strike is None: continue
        direction = b("type", str)  # "up" / "down"
        call_oi = b("oi_call", int, 0) or 0
        put_oi = b("oi_put", int, 0) or 0
        y_call_oi = b("y_oi_call", int, 0) or 0
        y_put_oi = b("y_oi_put", int, 0) or 0
        # os_call/os_put 是 "5.10%" 格式,去 % 轉 float
        def parse_pct(s):
            if not s: return 0.0
            return float(s.replace("%", "").strip())
        call_pct = parse_pct(b("os_call", str, "0"))
        put_pct = parse_pct(b("os_put", str, "0"))

        # 隔日變化
        call_oi_change = call_oi - y_call_oi if y_call_oi else 0
        put_oi_change = put_oi - y_put_oi if y_put_oi else 0

        # 計算相對位置
        rel_pos = strike - int(last_close) if last_close else 0
        distance_pct = round(rel_pos / last_close * 100, 2) if last_close else 0
        is_near_money = abs(rel_pos) <= NEAR_MONEY_THRESHOLD

        # 重貨區判定 (街貨 ≥ 5% 視為重貨)
        HEAVY_THRESHOLD = 5.0
        MOST_NEW_THRESHOLD = 200  # 隔日增加 > 200 視為「最多新增」
        call_flags = []
        if call_pct >= HEAVY_THRESHOLD: call_flags.append("重貨區")
        if call_oi_change >= MOST_NEW_THRESHOLD: call_flags.append("最多新增")
        put_flags = []
        if put_pct >= HEAVY_THRESHOLD: put_flags.append("重貨區")
        if put_oi_change >= MOST_NEW_THRESHOLD: put_flags.append("最多新增")

        strikes.append({
            "strike": strike,
            "type": direction,  # up / down
            "call_oi": call_oi,
            "put_oi": put_oi,
            "y_call_oi": y_call_oi,
            "y_put_oi": y_put_oi,
            "call_oi_change": call_oi_change,
            "put_oi_change": put_oi_change,
            "call_pct": call_pct,
            "put_pct": put_pct,
            "relative_position": rel_pos,
            "distance_pct": distance_pct,
            "is_near_money": is_near_money,
            "call_flags": call_flags,
            "put_flags": put_flags,
        })

    return {
        "last_close": last_close,
        "update_time": update_time,
        "total_call_oi": total_call,
        "total_put_oi": total_put,
        "max_oi": max_oi,
        "step_used": OI_STEP,
        "source_url": source_url,
        "strikes": strikes,
    }


def detect_edge_warning(last_close, strikes):
    """
    偵測 HSI 是否接近 API 範圍上下限
    回傳 warning 字串 ('' 表示無警告)
    """
    if not last_close or not strikes:
        return ""

    strikes_sorted = sorted([s["strike"] for s in strikes])
    lowest = strikes_sorted[0]
    highest = strikes_sorted[-1]

    # HSI 距上下限 < 1000 = 接近邊界
    near_lower = (last_close - lowest) < 1000
    near_upper = (highest - last_close) < 1000

    if near_lower and near_upper:
        return f"⚠️ HSI ({last_close:.0f}) 接近 API 顯示範圍上下限 ({lowest}-{highest}),請小心解讀"
    elif near_lower:
        return f"⚠️ HSI ({last_close:.0f}) 接近下限 ({lowest}),下方 OI 牆位資料可能不完整"
    elif near_upper:
        return f"⚠️ HSI ({last_close:.0f}) 接近上限 ({highest}),上方 OI 牆位資料可能不完整"
    return ""


def fallback_hkiei_7desl(target_date=None):
    """
    後備源: hkiei.com (iframe 跳到 7desl.com/hkex)
    資料源 CSV 列表:
      {date}/data-hsi-index.csv       - HSI 指數 (morning/afternoon/prev/change)
      {date}/hsi-options-months.csv   - 合约月份列表
      {date}/hsi-options-months-{MMM-YY}.csv  - 該月 OI 資料 (160 strikes)
      {date}/data-hsi-oi.csv          - 合约 OI 總結
      {date}/data-hsi-futures.csv     - 期貨資料

    回傳與主源相同結構的 dict,或 None (失敗)
    """
    if target_date is None:
        target_date = datetime.date.today()
    if isinstance(target_date, datetime.date):
        date_str = target_date.strftime("%Y-%m-%d")
    else:
        date_str = target_date

    base = f"{BACKUP_BASE}/{date_str}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": "https://hkiei.com/hsioptiondata/",
    }

    def fetch(path):
        url = f"{base}/{path}"
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code == 200:
                return r
            print(f"   ⚠️ {path}: HTTP {r.status_code}")
            return None
        except Exception as e:
            print(f"   ⚠️ {path}: {e}")
            return None

    # 1. 拿 HSI 收市
    r = fetch("data-hsi-index.csv")
    if not r: return None
    reader = csv.DictReader(io.StringIO(r.text))
    hsi_row = next(reader, None)
    if not hsi_row: return None
    try:
        last_close = float(hsi_row["afternoon_closing"])
    except (ValueError, KeyError):
        print(f"   ⚠️ 無法取 afternoon_closing: {r.text[:100]}")
        return None

    # 2. 拿合约月份 → 用第一個 (即月)
    r = fetch("hsi-options-months.csv")
    if not r: return None
    months = [m.strip() for m in r.text.strip().split("\n") if m.strip()]
    if not months:
        print("   ⚠️ 合约月份列表為空")
        return None
    front_month = months[0]  # 第一個 = 即月

    # 3. 拿即月 OI CSV
    r = fetch(f"hsi-options-months-{front_month}.csv")
    if not r: return None

    # CSV 結構 (25 columns):
    # Call (12): 開市,高,低,收市,價位增減,IV,成交增減,上日成交,成交,未平倉增減,上日未平倉,未平倉
    # Strike (1)
    # Put  (12): 未平倉,上日未平倉,未平倉增減,成交,上日成交,成交增減,IV,價位增減,收市,低,高,開市
    rows = list(csv.reader(io.StringIO(r.text)))
    if len(rows) < 2:
        print("   ⚠️ OI CSV 無資料")
        return None

    # 跳過 header (第一行)
    raw_strikes = []
    for row in rows[1:]:
        if len(row) < 25: continue
        try:
            strike = int(row[12])
            call_oi = int(row[11]) if row[11] else 0
            y_call_oi = int(row[10]) if row[10] else 0
            put_oi = int(row[13]) if row[13] else 0
            y_put_oi = int(row[14]) if row[14] else 0
            call_iv = float(row[5]) if row[5] else 0
            put_iv = float(row[19]) if row[19] else 0
            call_oi_change = call_oi - y_call_oi
            put_oi_change = put_oi - y_put_oi
            raw_strikes.append({
                "strike": strike,
                "call_oi": call_oi,
                "put_oi": put_oi,
                "y_call_oi": y_call_oi,
                "y_put_oi": y_put_oi,
                "call_oi_change": call_oi_change,
                "put_oi_change": put_oi_change,
                "call_iv": call_iv,
                "put_iv": put_iv,
            })
        except (ValueError, IndexError):
            continue

    if not raw_strikes:
        print("   ⚠️ 無法解析 OI CSV")
        return None

    total_call_oi = sum(s["call_oi"] for s in raw_strikes)
    total_put_oi = sum(s["put_oi"] for s in raw_strikes)

    # 重貨區判定
    HEAVY_THRESHOLD = 5.0
    MOST_NEW_THRESHOLD = 200

    strikes = []
    for s in raw_strikes:
        # 街貨 % = 該 strike OI / 全 call (or put) OI * 100
        call_pct = (s["call_oi"] / total_call_oi * 100) if total_call_oi else 0
        put_pct = (s["put_oi"] / total_put_oi * 100) if total_put_oi else 0
        rel_pos = s["strike"] - int(last_close)
        distance_pct = round(rel_pos / last_close * 100, 2) if last_close else 0
        is_near_money = abs(rel_pos) <= NEAR_MONEY_THRESHOLD
        call_flags = []
        if call_pct >= HEAVY_THRESHOLD: call_flags.append("重貨區")
        if s["call_oi_change"] >= MOST_NEW_THRESHOLD: call_flags.append("最多新增")
        put_flags = []
        if put_pct >= HEAVY_THRESHOLD: put_flags.append("重貨區")
        if s["put_oi_change"] >= MOST_NEW_THRESHOLD: put_flags.append("最多新增")
        direction = "up" if s["strike"] > last_close else "down"
        strikes.append({
            "strike": s["strike"],
            "type": direction,
            "call_oi": s["call_oi"],
            "put_oi": s["put_oi"],
            "y_call_oi": s["y_call_oi"],
            "y_put_oi": s["y_put_oi"],
            "call_oi_change": s["call_oi_change"],
            "put_oi_change": s["put_oi_change"],
            "call_pct": round(call_pct, 2),
            "put_pct": round(put_pct, 2),
            "call_iv": s["call_iv"],
            "put_iv": s["put_iv"],
            "relative_position": rel_pos,
            "distance_pct": distance_pct,
            "is_near_money": is_near_money,
            "call_flags": call_flags,
            "put_flags": put_flags,
        })

    # 按相對位置由遠到近再到遠負 → 上面最遠 → ... → 近 → ... → 下面最遠
    # 跟 hk.warrants.com 顯示順序一致
    strikes.sort(key=lambda x: -x["relative_position"])

    # update_time  - 拿 OI summary 拿成交日期
    r_oi = fetch("data-hsi-oi.csv")
    update_time = f"{date_str} 16:00:00"  # default
    if r_oi:
        try:
            reader = csv.DictReader(io.StringIO(r_oi.text))
            for row in reader:
                if row.get("Symbol") == "HSI" and row.get("Series", "").endswith(front_month):
                    # 結算日 = 收盘后
                    update_time = f"{date_str} 16:00:00 (settlement)"
                    break
        except Exception:
            pass

    return {
        "date": date_str,
        "update_time": update_time,
        "last_close": last_close,
        "total_call_oi": total_call_oi,
        "total_put_oi": total_put_oi,
        "max_oi": max((max(s["call_oi"], s["put_oi"]) for s in strikes), default=0),
        "step_used": 0,  # 7desl 固定 160 strikes,不用 step 概念
        "source_url": f"{base}/hsi-options-months-{front_month}.csv",
        "contract_month": front_month,
        "strikes": strikes,
    }


def upload_to_firestore(data):
    """上傳到 Firestore: market/hsi_options_oi"""
    if not _HAS_FIREBASE:
        print("⚠️ firebase_admin 未安裝,略過上傳 (本地測試)")
        return
    # 驗證: total_oi 為 0 的空資料不上傳 (避免重複上次 7/22 全 0 事件)
    total_oi = data.get("total_call_oi", 0) + data.get("total_put_oi", 0)
    if total_oi == 0:
        print(f"⚠️ total_oi=0,略過上傳 (避免 Firestore 存全 0 空資料)")
        return
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

        # 順便清掉舊的全 0 記錄 (以防之前上傳過)
        before = len(data_list)
        data_list = [d for d in data_list
                     if (d.get("total_call_oi", 0) + d.get("total_put_oi", 0)) > 0]
        if len(data_list) < before:
            print(f"   🧹 清掉 {before - len(data_list)} 筆全 0 舊記錄")

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


def build_distribution(data, call_pct, put_pct, edge_warning):
    """包裝成對前端友善的格式"""
    strikes = data["strikes"]
    return {
        "date": data["date"],
        "update_time": data["update_time"],
        "last_close": data["last_close"],
        "call_pct": round(call_pct, 1),
        "put_pct": round(put_pct, 1),
        "strike_count": len(strikes),
        "step_used": data.get("step_used", OI_STEP),
        "edge_warning": edge_warning,
        "data_fresh": data.get("data_fresh", True),
        "strikes": strikes,
    }


def main(target_date=None):
    """主爬取邏輯

    target_date: 留空用今天,指定時格式 'YYYY-MM-DD'
    抓取順序: hk.warrants.com (主) → 7desl.com/hkex (後備)
    """
    if target_date is None:
        target_date = datetime.date.today().strftime("%Y-%m-%d")

    print(f"📅 抓取 {target_date} 的 HSI 即月期權 OI 牆位...")

    data = None
    source = None

    # === 主源: hk.warrants.com XML API ===
    print(f"   🎯 主源: hk.warrants.com XML API (step={OI_STEP})")
    try:
        xml_text, url = fetch_oi_xml(target_date=target_date, step=OI_STEP)
        candidate = parse_oi_xml(xml_text, source_url=url)
        candidate["date"] = target_date
        # 驗證: HSI > 0 + 至少 10 個 strike + 總 OI > 0 (避免拿到全 0 的空資料)
        total_oi = candidate["total_call_oi"] + candidate["total_put_oi"]
        if candidate["last_close"] > 0 and len(candidate["strikes"]) >= 10 and total_oi > 0:
            candidate["data_fresh"] = True
            data = candidate
            source = "hk.warrants.com"
        else:
            print(f"   ⚠️ 主源回傳資料不足 (last={candidate['last_close']}, strikes={len(candidate['strikes'])}, total_oi={total_oi})")
    except Exception as e:
        print(f"   ⚠️ 主源失敗: {e}")

    # === 後備: 7desl.com/hkex (hkiei.com) ===
    if data is None:
        print(f"   🔄 後備源: 7desl.com/hkex (hkiei.com) - 101 strikes")
        try:
            candidate = fallback_hkiei_7desl(target_date=target_date)
            total_oi = (candidate.get("total_call_oi", 0) + candidate.get("total_put_oi", 0)) if candidate else 0
            if candidate and candidate["last_close"] > 0 and len(candidate["strikes"]) >= 10 and total_oi > 0:
                candidate["data_fresh"] = True
                data = candidate
                source = "7desl.com (hkiei.com)"
            else:
                print(f"   ⚠️ 後備源回傳資料不足 (last={candidate.get('last_close') if candidate else 'N/A'}, strikes={len(candidate['strikes']) if candidate else 0}, total_oi={total_oi})")
        except Exception as e:
            print(f"   ⚠️ 後備源失敗: {e}")

    if data is None:
        # 兩源今日資料不可用,回退取昨日 (兩源都是 T+1)
        if isinstance(target_date, str):
            tdate = datetime.date.fromisoformat(target_date)
        else:
            tdate = target_date
        yesterday = (tdate - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        # 跳過週末 (週五/六/日→ 走週五/五/三)
        # 但為簡化,週六日自動順延到週五
        weekday = tdate.weekday()  # 0=週一
        if weekday == 5:  # 週六
            yesterday = (tdate - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        elif weekday == 6:  # 週日
            yesterday = (tdate - datetime.timedelta(days=2)).strftime("%Y-%m-%d")
        # 週一到週五: 昨日是週一到週四,應該都有資料
        print(f"   🔄 今日 {target_date} 資料不可用,試昨日 {yesterday} ...")
        try:
            xml_text, url = fetch_oi_xml(target_date=yesterday, step=OI_STEP)
            candidate = parse_oi_xml(xml_text, source_url=url)
            candidate["date"] = yesterday  # 標記為實際資料日期
            candidate["data_fresh"] = False  # stale flag
            total_oi = candidate["total_call_oi"] + candidate["total_put_oi"]
            if candidate["last_close"] > 0 and len(candidate["strikes"]) >= 10 and total_oi > 0:
                data = candidate
                source = f"hk.warrants.com (昨日 {yesterday} 資料)"
        except Exception as e:
            print(f"   ⚠️ 昨日主源失敗: {e}")
        if data is None:
            try:
                candidate = fallback_hkiei_7desl(target_date=yesterday)
                if candidate:
                    candidate["date"] = yesterday
                    candidate["data_fresh"] = False
                total_oi = (candidate.get("total_call_oi", 0) + candidate.get("total_put_oi", 0)) if candidate else 0
                if candidate and candidate["last_close"] > 0 and len(candidate["strikes"]) >= 10 and total_oi > 0:
                    data = candidate
                    source = f"7desl.com (昨日 {yesterday} 資料)"
            except Exception as e:
                print(f"   ⚠️ 昨日後備失敗: {e}")

    if data is None:
        print(f"❌ 主源 + 後備 + 昨日回退都失敗,無法取得 {target_date} 資料")
        sys.exit(1)

    print(f"   ✅ 使用來源: {source}")

    data["call_pct"] = data["total_call_oi"]  # placeholder,計算用
    data["put_pct"] = data["total_put_oi"]

    last_close = data["last_close"]
    total_call = data["total_call_oi"]
    total_put = data["total_put_oi"]
    all_oi = total_call + total_put
    call_pct = (total_call / all_oi * 100) if all_oi else 50.0
    put_pct = (total_put / all_oi * 100) if all_oi else 50.0

    edge_warning = detect_edge_warning(last_close, data["strikes"])

    distribution = build_distribution(data, call_pct, put_pct, edge_warning)
    distribution["_source"] = source  # 記錄實際使用來源

    save_data(distribution)
    try:
        upload_to_firestore(distribution)
    except Exception as e:
        print(f"⚠️  Firestore 失敗但本地已存: {e}")

    s = distribution
    print(f"✅ 抓取完成:")
    print(f"   來源:        {s.get('_source', '?')}")
    print(f"   資料日期:    {s['date']}")
    print(f"   更新時間:    {s['update_time']}")
    print(f"   上日收市:    {s['last_close']:,.2f}")
    print(f"   認購街貨:    {s['call_pct']:.1f}%")
    print(f"   認沽街貨:    {s['put_pct']:.1f}%")
    print(f"   strike 檔數: {s['strike_count']}")
    if s.get('step_used', 0) > 0:
        print(f"   step:        {s['step_used']}")
    if s.get('contract_month'):
        print(f"   合約月:      {s['contract_month']}")
    print(f"   edge warning: {edge_warning or '(無)'}")
    print(f"📁 已存檔: {OUTPUT_FILE} + archive/options_oi_{s['date']}.json")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else None
    main(target_date=target)
