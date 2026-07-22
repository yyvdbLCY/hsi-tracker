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

# 主源 1 (HKEX 港交所 - 官方權威)
HKEX_BASE = "https://www.hkex.com.hk/eng/stat/dmstat/OI"

# 主源 2 (hk.warrants.com XML API)
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


def primary_hkex(target_date=None):
    """
    主源: 港交所 HKEX 官方資料 (最權威)
    URL: https://www.hkex.com.hk/eng/stat/dmstat/OI/DTOP_F_YYYYMMDD.zip
    ZIP 內容: 多個 .rpt + .raw 檔
      - yyyymmdd_1_dtop_f_hkcc_opt_dtl_hsi.rpt  (HSI options detail)
      - yyyymmdd_1_dtop_f_hkcc_fut_dtl_hsi.rpt  (HSI futures)
    報告檔名格式: REPORT DATE : DDMMMYY (e.g. 22JUL26)
    擋區格式 (HSI options):
      STRIKE | CALL_GROSS CALL_NET CALL_CHG CALL_TO CALL_DEAL CALL_SETTLE CALL_PRC_CHG
            | PUT_GROSS  PUT_NET  PUT_CHG  PUT_TO  PUT_DEAL  PUT_SETTLE  PUT_PRC_CHG

    T+0 (當日 20:59 HKT 後上傳)
    """
    if target_date is None:
        target_date = datetime.date.today()
    if isinstance(target_date, str):
        target_date_dt = datetime.date.fromisoformat(target_date)
    else:
        target_date_dt = target_date
    date_str = target_date_dt.strftime("%Y-%m-%d")
    # HKEX ZIP 檔名用 MMM-YY 格式 (e.g. DTOP_F_20260722.zip)
    zip_url = f"{HKEX_BASE}/DTOP_F_{date_str.replace('-', '')}.zip"
    page_url = f"{HKEX_BASE}/oi_f.asp"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": page_url,
    }

    # 1. 下載 ZIP
    try:
        resp = requests.get(zip_url, headers=headers, timeout=20)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code} for {zip_url}")
        zip_bytes = resp.content
    except Exception as e:
        raise RuntimeError(f"HKEX ZIP 下載失敗: {e}")

    # 2. 解壓
    import zipfile, io as iomod
    try:
        with zipfile.ZipFile(iomod.BytesIO(zip_bytes)) as z:
            # 找 HSI options file
            hsi_opt_files = [n for n in z.namelist() if "opt_dtl_hsi.rpt" in n and not n.endswith(".raw")]
            if not hsi_opt_files:
                raise RuntimeError("ZIP 內找不到 opt_dtl_hsi.rpt")
            with z.open(hsi_opt_files[0]) as f:
                rpt_text = f.read().decode("ascii", errors="ignore")
    except Exception as e:
        raise RuntimeError(f"HKEX ZIP 解壓失敗: {e}")

    # 3. 解析 options detail (raw strikes, call_oi/put_oi only)
    raw_strikes = parse_hkex_options_rpt(rpt_text, target_date_dt)

    if not raw_strikes:
        raise RuntimeError("HKEX options 解析結果為空")

    # 4. HSI 收市來源順序: 7desl → HKEX 期貨 settle → yfinance → median
    last_close = _fetch_hsi_close_from_7desl(target_date_dt)
    hsi_source = "7desl"
    if last_close <= 0:
        # 拿 HKEX 期貨 SETTLE 作為 proxy (官方,差距 <300 點)
        last_close = _fetch_hsi_close_from_hkex_futures(zip_bytes)
        hsi_source = "HKEX 期貨 settle (proxy)"
    if last_close <= 0:
        # yfinance
        last_close = _fetch_hsi_close_yfinance(target_date_dt)
        hsi_source = "yfinance ^HSI"
    if last_close <= 0:
        # 最後中位數 fallback
        non_zero_strikes = sorted([s["strike"] for s in raw_strikes])
        last_close = float(non_zero_strikes[len(non_zero_strikes) // 2])
        hsi_source = "median of strikes"

    # 5. 計算總 OI
    total_call_oi = sum(s["call_oi"] for s in raw_strikes)
    total_put_oi = sum(s["put_oi"] for s in raw_strikes)
    all_oi = total_call_oi + total_put_oi
    call_pct = (total_call_oi / all_oi * 100) if all_oi else 50.0
    put_pct = (total_put_oi / all_oi * 100) if all_oi else 50.0

    # 6. 後處理: 加相對位置 / is_near_money / 重貨區 / 街貨% / 排序
    strikes = finalize_hkex_strikes(raw_strikes, last_close, total_call_oi, total_put_oi)

    # 7. 取到期日 (即月 = 報告中第一個 EXPIRATION DATE)
    front_expiry = _extract_front_expiry(rpt_text)
    update_time = f"{date_str} 21:00:00 (HKEX 官方)"

    return {
        "date": date_str,
        "update_time": update_time,
        "last_close": last_close,
        "last_close_source": hsi_source,
        "total_call_oi": total_call_oi,
        "total_put_oi": total_put_oi,
        "call_pct": round(call_pct, 1),
        "put_pct": round(put_pct, 1),
        "max_oi": max((max(s["call_oi"], s["put_oi"]) for s in strikes), default=0),
        "step_used": 0,  # HKEX step 是固定的 (100)
        "source_url": zip_url,
        "contract_month": front_expiry,
        "strikes": strikes,
    }


def parse_hkex_options_rpt(text, target_date):
    """解析 HKEX options detail .rpt 檔
    只取第一個 EXPIRATION (即月)
    """
    lines = text.split("\n")
    current_expiry = None
    first_expiry = None
    strikes = []

    for line in lines:
        m = re.search(r"EXPIRATION DATE\s*:\s*(\S+ \S+)", line)
        if m:
            current_expiry = m.group(1).strip()
            if first_expiry is None:
                first_expiry = current_expiry
            continue
        # 只保留第一個 expiration 的 strike (檔案內同一個 expiration 跨多頁)
        if first_expiry is None or current_expiry != first_expiry:
            continue
        # 解析 data row
        m = re.match(r"^\s+(\d{1,3},?\d{3})\s+(.+)$", line)
        if not m:
            continue
        strike = int(m.group(1).replace(",", ""))
        fields = m.group(2).split()
        if len(fields) < 14:
            continue
        # 解析 14 個欄位
        def to_int(s):
            return int(s.replace(",", "")) if s else 0
        call_gross = to_int(fields[0])
        call_net = to_int(fields[1])
        call_chg = to_int(fields[2])
        call_to = to_int(fields[3])
        call_deal = to_int(fields[4])
        call_settle = to_int(fields[5])
        call_prc_chg = to_int(fields[6])
        put_gross = to_int(fields[7])
        put_net = to_int(fields[8])
        put_chg = to_int(fields[9])
        put_to = to_int(fields[10])
        put_deal = to_int(fields[11])
        put_settle = to_int(fields[12])
        put_prc_chg = to_int(fields[13])

        # 全部 0 的 (完全無 OI) 跳過,減少資料量
        if call_gross == 0 and put_gross == 0:
            continue

        strikes.append({
            "strike": strike,
            "type": "up",
            "call_oi": call_gross,
            "put_oi": put_gross,
            "y_call_oi": max(0, call_gross - call_chg),
            "y_put_oi": max(0, put_gross - put_chg),
            "call_oi_change": call_chg,
            "put_oi_change": put_chg,
            "call_settle": call_settle,
            "put_settle": put_settle,
            "call_pct": 0.0,
            "put_pct": 0.0,
            "call_iv": 0.0,
            "put_iv": 0.0,
            "relative_position": 0,
            "distance_pct": 0,
            "is_near_money": False,
            "call_flags": [],
            "put_flags": [],
        })

    return strikes


def _extract_front_expiry(text):
    """從 HKEX options 檔案抓第一個 EXPIRATION DATE
    原本 \S+ \S+ 只抓兩個詞,改用 .+ 才抓得到 '30 JUL 26'
    """
    m = re.search(r"EXPIRATION DATE\s*:\s*(.+?)\s*$", text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _fetch_hsi_close_from_7desl(target_date):
    """從 7desl 拿 HSI 收市 (HKEX ZIP 沒提供指數)
    來源順序:
      1. 7desl data-hsi-index.csv (最準, T+1 上傳)
      2. yfinance ^HSI (可能拿到 HSI 期貨,需範圍 + 變動過濾)
      3. 回傳 0 (頁面會顯示 --)
    """
    date_str = target_date.strftime("%Y-%m-%d") if isinstance(target_date, datetime.date) else target_date
    # 1. 7desl (T+1 上傳,當日資料通常 21:30 HKT 後)
    try:
        url = f"{BACKUP_BASE}/{date_str}/data-hsi-index.csv"
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://7desl.com/hkex"}, timeout=8)
        if resp.status_code == 200:
            reader = csv.DictReader(io.StringIO(resp.text))
            row = next(reader, None)
            if row and "afternoon_closing" in row:
                val = float(row["afternoon_closing"])
                if 20000 < val < 30000:
                    return val
    except Exception:
        pass
    # 2. yfinance fallback (^HSI) - 注意 1.5+ 可能返回 HSI 期貨
    # 期貨會被識別為「今日變動過大」,但 index 的每日變動多在 ±500 以內
    return _fetch_hsi_close_yfinance(target_date)


def _fetch_hsi_close_yfinance(target_date):
    """yfinance ^HSI 拿 HSI 指數
    注意 1.5+ 版可能返回 HSI 期貨,加變動過濾 (|Δ| < 500)
    """
    try:
        import yfinance as yf
        ticker = yf.Ticker("^HSI")
        if isinstance(target_date, datetime.date) and not isinstance(target_date, datetime.datetime):
            target_date = datetime.datetime.combine(target_date, datetime.time())
        end_d = target_date + datetime.timedelta(days=2)
        start_d = target_date - datetime.timedelta(days=3)
        hist = ticker.history(start=start_d.strftime("%Y-%m-%d"), end=end_d.strftime("%Y-%m-%d"))
        if hist is not None and len(hist) >= 2:
            latest = float(hist["Close"].iloc[-1])
            prev = float(hist["Close"].iloc[-2])
            if 20000 < latest < 30000 and abs(latest - prev) < 500:
                return latest
    except Exception:
        pass
    return 0.0


def _fetch_hsi_close_from_hkex_futures(zip_bytes):
    """從 HKEX options ZIP 拿 HSI 30 JUL 26 期貨 SETTLE 作為 HSI close proxy
    HSI 期貨 SETTLE 是 HKEX 官方公布,跟 HSI close 差距通常 < 300 點
    """
    try:
        import zipfile, io as iomod
        with zipfile.ZipFile(iomod.BytesIO(zip_bytes)) as z:
            # 找 HSI futures file
            fut_files = [n for n in z.namelist() if "fut_dtl_hsi.rpt" in n and not n.endswith(".raw")]
            if not fut_files:
                return 0.0
            with z.open(fut_files[0]) as f:
                text = f.read().decode("ascii", errors="ignore")
        # 找第一個 HSI 即月 (30 JUL 26) SETTLE
        # 格式: HSI    30 JUL 26    ...    SETTLE  PRICE  PRICE CHANGE
        m = re.search(r"HSI\s+30 JUL \d+\s+[\d,]+\s+[\d,]+\s+[\d,\-]+\s+[\d,]+\s+[\d,]+\s+([\d,]+)", text)
        if m:
            return float(m.group(1).replace(",", ""))
    except Exception:
        pass
    return 0.0


def finalize_hkex_strikes(strikes, last_close, total_call_oi, total_put_oi):
    """HKEX strikes 後處理: 加相對位置 / is_near_money / 重貨 / 街貨%"""
    HEAVY_THRESHOLD = 5.0
    MOST_NEW_THRESHOLD = 200
    out = []
    for s in strikes:
        rel_pos = s["strike"] - int(last_close)
        distance_pct = round(rel_pos / last_close * 100, 2) if last_close else 0
        is_near_money = abs(rel_pos) <= NEAR_MONEY_THRESHOLD
        call_pct = (s["call_oi"] / total_call_oi * 100) if total_call_oi else 0
        put_pct = (s["put_oi"] / total_put_oi * 100) if total_put_oi else 0
        call_flags = []
        if call_pct >= HEAVY_THRESHOLD: call_flags.append("重貨區")
        if s["call_oi_change"] >= MOST_NEW_THRESHOLD: call_flags.append("最多新增")
        put_flags = []
        if put_pct >= HEAVY_THRESHOLD: put_flags.append("重貨區")
        if s["put_oi_change"] >= MOST_NEW_THRESHOLD: put_flags.append("最多新增")
        out.append({
            "strike": s["strike"],
            "type": "up" if rel_pos > 0 else "down",
            "call_oi": s["call_oi"],
            "put_oi": s["put_oi"],
            "y_call_oi": s["y_call_oi"],
            "y_put_oi": s["y_put_oi"],
            "call_oi_change": s["call_oi_change"],
            "put_oi_change": s["put_oi_change"],
            "call_settle": s["call_settle"],
            "put_settle": s["put_settle"],
            "call_pct": round(call_pct, 2),
            "put_pct": round(put_pct, 2),
            "call_iv": 0.0,
            "put_iv": 0.0,
            "relative_position": rel_pos,
            "distance_pct": distance_pct,
            "is_near_money": is_near_money,
            "call_flags": call_flags,
            "put_flags": put_flags,
        })
    # 排序: 上面最遠 → ... → 近 → ... → 下面最遠 (跟其他源一致)
    out.sort(key=lambda x: -x["relative_position"])
    return out


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
        "total_call_oi": data.get("total_call_oi", 0),
        "total_put_oi": data.get("total_put_oi", 0),
        "max_oi": data.get("max_oi", 0),
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

    # === 主源 1: 港交所 HKEX 官方資料 ===
    print(f"   🎯 主源 1: HKEX 港交所 (最權威, T+0 21:00 HKT 上傳)")
    try:
        candidate = primary_hkex(target_date=target_date)
        total_oi = candidate.get("total_call_oi", 0) + candidate.get("total_put_oi", 0)
        if candidate.get("last_close", 0) > 0 and len(candidate.get("strikes", [])) >= 10 and total_oi > 0:
            candidate["data_fresh"] = True
            data = candidate
            source = "HKEX 港交所 (官方)"
        else:
            print(f"   ⚠️ HKEX 回傳資料不足 (last={candidate.get('last_close')}, strikes={len(candidate.get('strikes', []))}, total_oi={total_oi})")
    except Exception as e:
        print(f"   ⚠️ HKEX 失敗: {str(e)[:120]}")

    # === 主源 2: hk.warrants.com XML API ===
    if data is None:
        print(f"   🔄 主源 2: hk.warrants.com XML API (step={OI_STEP})")
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
                print(f"   ⚠️ 主源 2 回傳資料不足 (last={candidate['last_close']}, strikes={len(candidate['strikes'])}, total_oi={total_oi})")
        except Exception as e:
            print(f"   ⚠️ 主源 2 失敗: {e}")

    # === 後備 3: 7desl.com/hkex (hkiei.com) ===
    if data is None:
        print(f"   🔄 後備 3: 7desl.com/hkex (hkiei.com) - 101 strikes")
        try:
            candidate = fallback_hkiei_7desl(target_date=target_date)
            total_oi = (candidate.get("total_call_oi", 0) + candidate.get("total_put_oi", 0)) if candidate else 0
            if candidate and candidate["last_close"] > 0 and len(candidate["strikes"]) >= 10 and total_oi > 0:
                candidate["data_fresh"] = True
                data = candidate
                source = "7desl.com (hkiei.com)"
            else:
                print(f"   ⚠️ 後備 3 回傳資料不足 (last={candidate.get('last_close') if candidate else 'N/A'}, strikes={len(candidate['strikes']) if candidate else 0}, total_oi={total_oi})")
        except Exception as e:
            print(f"   ⚠️ 後備 3 失敗: {e}")

    if data is None:
        # 兩源今日資料不可用,回退取昨日 (都是 T+1, 週五六自動跳)
        if isinstance(target_date, str):
            tdate = datetime.date.fromisoformat(target_date)
        else:
            tdate = target_date
        yesterday = (tdate - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        weekday = tdate.weekday()
        if weekday == 5:  # 週六
            yesterday = (tdate - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        elif weekday == 6:  # 週日
            yesterday = (tdate - datetime.timedelta(days=2)).strftime("%Y-%m-%d")
        print(f"   🔄 今日 {target_date} 資料不可用,試昨日 {yesterday} ...")
        # 昨日: HKEX → hk.warrants → 7desl
        try:
            candidate = primary_hkex(target_date=yesterday)
            total_oi = candidate.get("total_call_oi", 0) + candidate.get("total_put_oi", 0)
            if candidate.get("last_close", 0) > 0 and len(candidate.get("strikes", [])) >= 10 and total_oi > 0:
                candidate["date"] = yesterday
                candidate["data_fresh"] = False
                data = candidate
                source = f"HKEX 港交所 (昨日 {yesterday} 資料)"
        except Exception as e:
            print(f"   ⚠️ 昨日 HKEX 失敗: {str(e)[:80]}")
        if data is None:
            try:
                xml_text, url = fetch_oi_xml(target_date=yesterday, step=OI_STEP)
                candidate = parse_oi_xml(xml_text, source_url=url)
                candidate["date"] = yesterday
                candidate["data_fresh"] = False
                total_oi = candidate["total_call_oi"] + candidate["total_put_oi"]
                if candidate["last_close"] > 0 and len(candidate["strikes"]) >= 10 and total_oi > 0:
                    data = candidate
                    source = f"hk.warrants.com (昨日 {yesterday} 資料)"
            except Exception as e:
                print(f"   ⚠️ 昨日主源 2 失敗: {e}")
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
        print(f"❌ HKEX + hk.warrants + 7desl + 昨日回退都失敗,無法取得 {target_date} 資料")
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
