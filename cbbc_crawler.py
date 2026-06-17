import json
import requests
import time
import datetime
import os

# 法兴街货 API
API_URL = "https://hk.warrants.com/hk/data/chart/stock_cbbc_real2.cgi"
OUTPUT_FILE = "cbbc_distribution.json"

# 校正系数（你已验证过，用于修正 500 点内重货牛证数量）
CORRECTION_FACTOR = 1.713

def fetch_cbbc_data():
    """请求 API 并返回原始数据（包含 mainData）"""
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
        "X-Requested-With": "XMLHttpRequest"
    }
    resp = requests.get(API_URL, params=params, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()

def parse_distribution(raw_data):
    """
    从原始 API 响应中提取：
    - 每个行使价区间的牛熊证街货量（列表）
    - 总牛证、总熊证
    - 恒指现价
    - 日期
    """
    fd = raw_data.get("furtherData", {})
    hsi_last = float(fd.get("hsilast", 0))
    data_date = fd.get("sdate", datetime.date.today().strftime("%Y-%m-%d"))

    # 处理 mainData，构建分布列表
    distribution = []
    sum_bull = 0
    sum_bear = 0
    bull_500_sum = 0

    for item in raw_data.get("mainData", []):
        ty = item.get("ty")  # 'bull' 或 'bear'
        try:
            volume = int(round(float(item.get("o1", 0))))  # 街货量
        except (ValueError, TypeError):
            volume = 0
        if volume == 0:
            continue

        fr = item.get("fr")   # 区间下限
        to = item.get("to")   # 区间上限
        if fr is None or to is None:
            continue

        # 记录分布点（取区间中点作为行使价代表）
        strike = (fr + to) / 2

        if ty == "bull":
            sum_bull += volume
            # 500 点内重货牛证：区间下限在现价-500 到现价之间
            if fr >= (hsi_last - 500) and fr <= hsi_last:
                bull_500_sum += volume
            distribution.append({
                "type": "bull",
                "strike": round(strike, 2),
                "low": fr,
                "high": to,
                "volume": volume
            })
        else:  # bear
            sum_bear += volume
            distribution.append({
                "type": "bear",
                "strike": round(strike, 2),
                "low": fr,
                "high": to,
                "volume": volume
            })

    # 应用校正系数到 500 点牛证
    bull_500_corrected = int(round(bull_500_sum * CORRECTION_FACTOR))

    # 计算牛证占比
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

def save_data(data):
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✅ 数据已保存至 {OUTPUT_FILE}")

if __name__ == "__main__":
    print(f"📅 开始抓取 {datetime.date.today().isoformat()} 的街货分布...")
    raw = fetch_cbbc_data()
    if raw is None:
        print("❌ 无法获取数据，任务终止。")
        exit(1)

    parsed = parse_distribution(raw)
    save_data(parsed)

    s = parsed["summary"]
    print(f"📊 总牛证: {s['total_bull']:,} | 总熊证: {s['total_bear']:,}")
    print(f"🎯 500点内重货牛证: {s['bull_500']:,} (校正后)")
    print(f"📈 恒指现价: {parsed['hsi']}")
    print(f"📁 分布数据包含 {len(parsed['distribution'])} 个档位")
