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
if not firebase_admin._apps:
    cred = credentials.Certificate("serviceAccountKey.json")
    firebase_admin.initialize_app(cred)
db = firestore.client()
doc_ref = db.collection("market").document("hsi_data")

def upload_to_firestore(data):
    """将爬虫结果转换为前端兼容格式并合并到 Firestore"""
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

# ---------- 以下为你原有代码（完整保留） ----------
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
    """
    从港交所下载并解析牛熊证日报。
    若 hsi_last 为 None，则自行估算（回退方案）。
    返回 (data_dict, None) 或 (None, error_msg)
    """
    date_str = target_date.strftime("%y%m%d")
    url = f"https://www.hkex.com.hk/chi/stat/dmstat/dayrpt/hsirrcbc{date_str}.zip"
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            # 找到包含 'cbbc' 的 CSV 文件
            csv_name = None
            for name in zf.namelist():
                if 'cbbc' in name.lower() and name.endswith('.csv'):
                    csv_name = name
                    break
            if not csv_name:
                raise ValueError("ZIP 中未找到 CBBC CSV 文件")

            with zf.open(csv_name) as f:
                df = pd.read_csv(f, encoding='big5', skipfooter=2, engine='python')

        # ---------- 字段映射（根据实际 CSV 调整）----------
        col_map = {
            'type': '牛熊證類別',
            'strike': '行使價',
            'call_level': '收回價',
            'volume_m': '街貨量(百萬份)',
        }
        df = df.rename(columns={
            col_map['type']: 'type',
            col_map['strike']: 'strike',
            col_map['call_level']: 'call_level',
            col_map['volume_m']: 'volume_m',
        })

        # 只保留恒指产品
        if '相關資產編號' in df.columns:
            df = df[df['相關資產編號'].str.strip() == 'HSI']

        # 类型转换
        df['strike'] = pd.to_numeric(df['strike'], errors='coerce')
        df['volume_m'] = pd.to_numeric(df['volume_m'], errors='coerce')
        df['type'] = df['type'].str.strip()
        df['volume'] = (df['volume_m'] * 1_000_000).fillna(0).astype(int)

        # 分离牛熊
        df_bull = df[df['type'] == '牛'].copy()
        df_bear = df[df['type'] == '熊'].copy()

        # ---------- 按 100 点区间聚合 ----------
        def group_100(data, bs_type):
            if data.empty:
                return [], 0
            data['low'] = (data['strike'] // 100 * 100).astype(int)
            data['high'] = data['low'] + 100
            grp = data.groupby(['low', 'high']).agg(
                volume=('volume', 'sum'),
                strike_avg=('strike', 'mean')
            ).reset_index()
            dist = []
            for _, row in grp.iterrows():
                dist.append({
                    "type": bs_type,
                    "strike": round(row['strike_avg'], 2),
                    "low": int(row['low']),
                    "high": int(row['high']),
                    "volume": int(row['volume'])
                })
            return dist, int(grp['volume'].sum())

        bull_dist, sum_bull = group_100(df_bull, 'bull')
        bear_dist, sum_bear = group_100(df_bear, 'bear')
        distribution = bull_dist + bear_dist

        total = sum_bull + sum_bear
        bull_pct = round(sum_bull / total * 100, 1) if total > 0 else 50.0

        # ---------- 恒指现价处理 ----------
        if hsi_last is None or hsi_last <= 0:
            # 没有从法兴拿到现价，用街货量最大收回价估算
            hsi_est = 0
            if not df_bull.empty and not df_bear.empty:
                top_bull = df_bull.nlargest(1, 'volume')['call_level'].values[0]
                top_bear = df_bear.nlargest(1, 'volume')['call_level'].values[0]
                hsi_est = (top_bull + top_bear) / 2
            hsi_last = round(hsi_est, 2)

        # 500点内重货牛证
        bull_500_sum = 0
        if hsi_last > 0:
            mask = (df_bull['strike'] >= hsi_last - 500) & (df_bull['strike'] <= hsi_last)
            bull_500_sum = int(df_bull.loc[mask, 'volume'].sum())

        bull_500_corrected = int(round(bull_500_sum * CORRECTION_FACTOR))

        result = {
            "date": target_date.isoformat(),
            "hsi": hsi_last,
            "summary": {
                "total_bull": sum_bull,
                "total_bear": sum_bear,
                "bull_pct": bull_pct,
                "bull_500": bull_500_corrected
            },
            "distribution": distribution
        }
        return result, None

    except Exception as e:
        return None, str(e)

def fallback_sg_full(target_date=None):
    """完全回退到法兴 API，使用官方汇总数据
    target_date: 指定補捉日期 (date 物件) ；None = 今天
    """
    if target_date is None:
        target_date = datetime.date.today()
    sdate_param = target_date.strftime("%y%m%d")
    
    API_URL = "https://hk.warrants.com/hk/data/chart/stock_cbbc_real2.cgi"
    params = {"ucode": "HSI", "spread": "100", "sdate": sdate_param, "_": int(time.time() * 1000)}
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://hk.warrants.com/tc/cbbc/outstanding-distribution"}
    resp = requests.get(API_URL, params=params, headers=headers, timeout=15)
    resp.raise_for_status()
    raw = resp.json()

    fd = raw.get("furtherData", {})
    hsi_last = float(fd.get("hsilast", 0))
    # 使用請求的日期當作 data_date (保證日期標記正確)
    data_date = target_date.isoformat()
    sum_bull = int(fd.get("sumBull", 0))
    sum_bear = int(fd.get("sumBear", 0))

    # 计算分布（保留你原有邏輯，用於存檔）
    distribution = []
    for item in raw.get("mainData", []):
        ty = item.get("ty")
        try:
            volume = int(round(float(item.get("o1", 0))))
        except (ValueError, TypeError):
            continue
        if volume == 0: continue
        fr = item.get("fr")
        to = item.get("to")
        if fr is None or to is None: continue
        strike = (fr + to) / 2
        distribution.append({
            "type": "bull" if ty == "bull" else "bear",
            "strike": round(strike, 2),
            "low": fr,
            "high": to,
            "volume": volume
        })

    # 500点内重货牛证：累加所有 strike 在 [hsi-500, hsi] 區間的牛證 volume
    bull_500_sum = 0
    if hsi_last > 0:
        for d in distribution:
            if d["type"] == "bull" and d["strike"] >= hsi_last - 500 and d["strike"] <= hsi_last:
                bull_500_sum += d["volume"]
    # 與 hkex 數據源保持一致 (CORRECTION_FACTOR = 1.0)
    bull_500_corrected = int(round(bull_500_sum * 1.0))

    total = sum_bull + sum_bear
    bull_pct = round(sum_bull / total * 100, 1) if total > 0 else 50.0

    return {
        "date": data_date,
        "hsi": hsi_last,
        "summary": {
            "total_bull": sum_bull,
            "total_bear": sum_bear,
            "bull_pct": bull_pct,
            "bull_500": bull_500_corrected
        },
        "distribution": distribution
    }


def fallback_bnp_paribas(target_date=None):
    """備援: BNP Paribas (法巴) CBBC 街貨 API
    URL: https://www.bnppwarrant.com/tc/data/json/cbbc-band-json-all/ucode/HSI/step/15/spread/100
    優勢: JSON API, 含 10 天歷史, 不需 Playwright
    """
    if target_date is None:
        target_date = datetime.date.today()
    sdate_param = target_date.strftime("%y%m%d")

    API_URL = "https://www.bnppwarrant.com/tc/data/json/cbbc-band-json-all/ucode/HSI/step/15/spread/100"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.bnppwarrant.com/tc/cbbc/outstanding-distribution",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }
    resp = requests.get(API_URL, params={"sdate": sdate_param}, headers=headers, timeout=15)
    resp.raise_for_status()
    raw = resp.json()

    fd = raw.get("furtherData", {})
    hsi_last = float(fd.get("hsilast", 0))
    data_date = target_date.isoformat()
    sum_bull = int(round(float(fd.get("sumBull", 0))))
    sum_bear = int(round(float(fd.get("sumBear", 0))))

    # udates 陣列中, 找最接近 target_date 的日期作為實際 data_date
    udates = fd.get("udates", [])
    if udates and target_date.isoformat() not in udates:
        # 找最接近且 ≤ target_date 的日期
        for d in sorted(udates, reverse=True):
            if d <= target_date.isoformat():
                data_date = d
                break
        # 同步 HSI 跟 udates 對應
        ulast = fd.get("ulast_his", [])
        try:
            idx = udates.index(data_date)
            if idx < len(ulast):
                hsi_last = float(ulast[idx])
        except (ValueError, IndexError):
            pass

    # 構造 distribution
    distribution = []
    for item in raw.get("mainData", []):
        try:
            volume = int(round(float(item.get("o1", 0))))
        except (ValueError, TypeError):
            continue
        if volume == 0:
            continue
        fr = item.get("fr")
        to = item.get("to")
        if fr is None or to is None:
            continue
        strike = (fr + to) / 2
        distribution.append({
            "type": "bull" if item.get("ty") == "bull" else "bear",
            "strike": round(strike, 2),
            "low": fr,
            "high": to,
            "volume": volume,
        })

    # 500 點內牛證
    bull_500_sum = 0
    if hsi_last > 0:
        for d in distribution:
            if d["type"] == "bull" and d["strike"] >= hsi_last - 500 and d["strike"] <= hsi_last:
                bull_500_sum += d["volume"]
    bull_500_corrected = int(round(bull_500_sum * 1.0))

    total = sum_bull + sum_bear
    bull_pct = round(sum_bull / total * 100, 1) if total > 0 else 50.0

    return {
        "date": data_date,
        "hsi": hsi_last,
        "summary": {
            "total_bull": sum_bull,
            "total_bear": sum_bear,
            "bull_pct": bull_pct,
            "bull_500": bull_500_corrected,
        },
        "distribution": distribution,
        "_source": "BNP Paribas (法巴)",
    }


def save_data(data, archive=True):
    # 主文件
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    # 历史备份
    if archive:
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        date_str = data["date"]
        archive_path = os.path.join(ARCHIVE_DIR, f"cbbc_{date_str}.json")
        with open(archive_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

def main():
    today = datetime.date.today()
    print(f"📅 开始获取 {today.isoformat()} 的牛熊证数据...")

    # 主源: 法興
    data = None
    source = ""
    try:
        data = fallback_sg_full()
        if data["summary"]["total_bull"] > 0:
            source = "法兴"
            print(f"✅ 法興獲取成功")
        else:
            print(f"⚠️ 法興返回空數據，改用 BNP Paribas 備援")
            data = None
    except Exception as e:
        print(f"⚠️ 法興獲取失敗 ({e})，改用 BNP Paribas 備援")

    # 備援: BNP Paribas
    if data is None:
        try:
            data = fallback_bnp_paribas()
            if data["summary"]["total_bull"] > 0:
                source = "BNP Paribas (法巴)"
                print(f"✅ BNP Paribas 備援成功")
            else:
                print(f"❌ BNP Paribas 也返回空數據")
                return
        except Exception as e:
            print(f"❌ BNP Paribas 備援也失敗: {e}")
            return

    data["_source"] = source
    save_data(data, archive=True)
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
