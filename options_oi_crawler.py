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
import firebase_admin
from firebase_admin import credentials, firestore
import requests

# ================= 配置 =================
# step=50 → 85 strikes (覆蓋 HSI ±28%),實務夠用且不過長
OI_STEP = 50

API_BASE = "https://hk.warrants.com/hk/data/chart/cbbc_oichart.cgi"
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
        "strikes": strikes,
    }


def main(target_date=None):
    """主爬取邏輯

    target_date: 留空用今天,指定時格式 'YYYY-MM-DD'
    """
    if target_date is None:
        target_date = datetime.date.today().strftime("%Y-%m-%d")

    print(f"📅 抓取 {target_date} 的 HSI 即月期權 OI 牆位 (XML API, step={OI_STEP})...")

    try:
        xml_text, url = fetch_oi_xml(target_date=target_date, step=OI_STEP)
    except Exception as e:
        print(f"❌ API 抓取失敗: {e}")
        sys.exit(1)

    data = parse_oi_xml(xml_text, source_url=url)
    data["date"] = target_date
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

    save_data(distribution)
    try:
        upload_to_firestore(distribution)
    except Exception as e:
        print(f"⚠️  Firestore 失敗但本地已存: {e}")

    s = distribution
    print(f"✅ 抓取完成:")
    print(f"   資料日期:    {s['date']}")
    print(f"   更新時間:    {s['update_time']}")
    print(f"   上日收市:    {s['last_close']:,.2f}")
    print(f"   認購街貨:    {s['call_pct']:.1f}%")
    print(f"   認沽街貨:    {s['put_pct']:.1f}%")
    print(f"   strike 檔數: {s['strike_count']} (step={s['step_used']})")
    print(f"   edge warning: {edge_warning or '(無)'}")
    print(f"📁 已存檔: {OUTPUT_FILE} + archive/options_oi_{s['date']}.json")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else None
    main(target_date=target)
