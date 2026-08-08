# -*- coding: utf-8 -*-
"""
股票复盘工作台 · 数据采集脚本（多源容错版）
============================================
数据源策略（按实测稳定性选型）：
  - 指数快照 / 自选股行情 / 历史K线  -> 腾讯行情接口（qt.gtimg.cn / ifzq.gtimg.cn，稳定）
  - 板块涨幅榜                       -> 同花顺（akshare）
  - 财经日历(宏观事件)                -> 百度股市通（akshare，未来21天）
  - 限售解禁                         -> 东方财富（akshare，未来45天）
  - 业绩预告                         -> 东方财富（akshare）

输出 data/ 下 JSON，供工作台 index.html 展示。
隐私说明：只采集公开行情，个人持仓/笔记不入库；data/watchlist.txt 会随仓库公开，勿放敏感信息。
"""

import json
import os
import time
import traceback
from datetime import datetime, timedelta

import akshare as ak
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

NOW = datetime.now()
TODAY = NOW.strftime("%Y%m%d")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

CAL_DAYS = 21      # 财经日历前瞻天数
UNLOCK_DAYS = 45   # 解禁前瞻天数
HIST_DAYS = 90     # 历史走势天数

INDEX_MAP = [  # (名称, 腾讯代码)
    ("上证指数", "sh000001"),
    ("深证成指", "sz399001"),
    ("创业板指", "sz399006"),
    ("科创50", "sh000688"),
    ("沪深300", "sh000300"),
]


def safe(fn):
    try:
        return fn()
    except Exception as e:
        print(f"[WARN] {fn.__name__} 失败: {type(e).__name__} {str(e)[:100]}")
        return None


def save_json(name, obj):
    with open(os.path.join(DATA_DIR, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    print(f"[OK] {name} ({len(obj) if isinstance(obj, list) else 'obj'})")


def tx_code(code):
    """A股代码转腾讯格式：6->sh, 0/3->sz, 其余->bj"""
    if code.startswith("6"):
        return "sh" + code
    if code.startswith(("0", "3")):
        return "sz" + code
    return "bj" + code


# ---------- 1. 指数快照（腾讯） ----------
def collect_index():
    q = ",".join(c for _, c in INDEX_MAP)
    r = requests.get("https://qt.gtimg.cn/q=" + q, headers=UA, timeout=15)
    r.encoding = "gbk"
    result = []
    for line in r.text.strip().split(";"):
        if "=" not in line:
            continue
        payload = line.split('="')[1].rstrip('"')
        f = payload.split("~")
        if len(f) < 40:
            continue
        result.append({
            "name": f[1], "code": f[2],
            "price": round(float(f[3]), 2),
            "pct": round(float(f[32]), 2),
            "chg": round(float(f[31]), 2),
            "amount": round(float(f[37]) / 10000, 1),  # 万元->亿元
        })
    return result


# ---------- 2. 板块涨幅榜（同花顺） ----------
def collect_sector():
    df = ak.stock_board_industry_summary_ths()
    df = df.sort_values("涨跌幅", ascending=False).head(10)
    result = []
    for _, r in df.iterrows():
        result.append({
            "name": str(r.get("板块", "")),
            "pct": round(float(r.get("涨跌幅", 0) or 0), 2),
            "leader": str(r.get("领涨股", "") or ""),
            "leader_pct": round(float(r.get("领涨股-涨跌幅", 0) or 0), 2),
            "up_count": int(r.get("上涨家数", 0) or 0),
            "down_count": int(r.get("下跌家数", 0) or 0),
        })
    return result


# ---------- 3. 财经日历（百度股市通，未来21天） ----------
# 关键词命中直接判"高"（对A股投资者最核心的宏观事件）
KEY_HIGH = ["CPI", "PPI", "LPR", "FOMC", "议息", "PMI", "GDP", "社融", "M2", "MLF",
            "降准", "降息", "美联储", "非农", "零售销售", "耐用品订单"]


def collect_econ_calendar():
    result = []
    for i in range(1, CAL_DAYS + 1):
        d = (NOW + timedelta(days=i)).strftime("%Y%m%d")
        try:
            df = ak.news_economic_baidu(date=d)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        day_rows = []
        for _, r in df.iterrows():
            try:
                imp = int(r.get("重要性", 0))
            except Exception:
                imp = 0
            country = str(r.get("地区", "") or "")
            event = str(r.get("事件", "") or "")
            # 中美核心宏观事件 → 高；其他中国/美国数据 → 中（对A股最相关）
            if ("中国" in country or "美国" in country) and any(k in event.upper() for k in KEY_HIGH):
                imp = max(imp, 3)
            elif "中国" in country:
                imp = max(imp, 2)
            elif "美国" in country:
                imp = max(imp, 2)
            day_rows.append({
                "date": str(r.get("日期", ""))[:10],
                "time": str(r.get("时间", "") or ""),
                "country": country,
                "importance": "高" if imp >= 3 else ("中" if imp == 2 else "低"),
                "event": event,
                "forecast": str(r.get("预期", "") or ""),
                "previous": str(r.get("前值", "") or ""),
            })
        # 每天降噪：中/高全保留，低只留前5条
        day_rows.sort(key=lambda x: (-(x["importance"] == "高"), -(x["importance"] == "中"), x["time"]))
        mid_hi = [x for x in day_rows if x["importance"] != "低"]
        lo_rows = [x for x in day_rows if x["importance"] == "低"][:5]
        result.extend(mid_hi + lo_rows)
        time.sleep(0.3)
    result.sort(key=lambda x: (x["date"], x["time"]))
    return result[:80]


# ---------- 4. 限售解禁（东财，未来45天，市值排序前25） ----------
def collect_unlock():
    start = TODAY
    end = (NOW + timedelta(days=UNLOCK_DAYS)).strftime("%Y%m%d")
    df = ak.stock_restricted_release_detail_em(start_date=start, end_date=end)
    if df is None or df.empty:
        return []
    result = []
    for _, r in df.iterrows():
        value = float(r.get("实际解禁市值", 0) or 0)
        ratio = float(r.get("占解禁前流通市值比例", 0) or 0)
        result.append({
            "date": str(r.get("解禁时间", ""))[:10],
            "code": str(r.get("股票代码", "") or ""),
            "name": str(r.get("股票简称", "") or ""),
            "unlock_value": round(value / 1e8, 2),   # 元->亿元
            "ratio": round(ratio * 100, 2),
            "type": str(r.get("限售股类型", "") or ""),
        })
    result.sort(key=lambda x: x["unlock_value"], reverse=True)
    return result[:25]


# ---------- 4.5 情绪温度计（涨停/跌停/炸板池，自动回退最近交易日） ----------
def _find_zt_date():
    """返回最近一个有涨停池数据的交易日 (date_str, df)"""
    for i in range(0, 12):
        d = (NOW - timedelta(days=i)).strftime("%Y%m%d")
        try:
            df = ak.stock_zt_pool_em(date=d)
            if df is not None and not df.empty:
                return d, df
        except Exception:
            continue
    return None, None


def collect_sentiment():
    date_str, zt_df = _find_zt_date()
    if not date_str:
        return {}
    zt = len(zt_df)
    dt = zb = 0
    try:
        df = ak.stock_zt_pool_dtgc_em(date=date_str)
        dt = len(df) if df is not None else 0
    except Exception:
        pass
    try:
        df = ak.stock_zt_pool_zbgc_em(date=date_str)
        zb = len(df) if df is not None else 0
    except Exception:
        pass
    # 连板分布
    lb_dist = {}
    if "连板数" in zt_df.columns:
        for v in zt_df["连板数"].dropna():
            v = int(v)
            lb_dist[v] = lb_dist.get(v, 0) + 1
    max_lb = max(lb_dist.keys(), default=1)
    # 炸板率
    zb_rate = round(zb / (zt + zb) * 100, 1) if (zt + zb) else 0.0
    # 情绪评分：涨停家数50% + 最高板30% + 炸板率20%
    zt_score = min(100, zt * 1.2)
    lb_score = min(100, max_lb * 14)
    zb_score = max(0, 100 - zb_rate * 3)
    score = round(0.5 * zt_score + 0.3 * lb_score + 0.2 * zb_score)
    level = "过热" if score >= 85 else ("高潮" if score >= 70 else ("发酵" if score >= 55 else ("复苏" if score >= 40 else "冰点")))
    note = ""
    if zb_rate > 40:
        note = "炸板率偏高，注意分歧"
    if dt >= 10:
        note = (note + "；" if note else "") + "跌停家数较多，情绪走弱"
    # 涨停行业分布 TOP6
    industries = []
    if "所属行业" in zt_df.columns:
        for name, cnt in zt_df["所属行业"].value_counts().head(6).items():
            industries.append({"name": str(name), "count": int(cnt)})
    return {
        "date": f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}",
        "zt": zt, "dt": dt, "zb": zb, "zb_rate": zb_rate,
        "max_lb": max_lb, "lb_dist": [{"lb": k, "count": v} for k, v in sorted(lb_dist.items())],
        "score": score, "level": level, "note": note, "industries": industries,
    }


# ---------- 5. 机构调研排行（东财 datacenter 直连，近7天 TOP8） ----------
def collect_research():
    """按股票聚合近7天机构调研：SUM=单次调研机构家数峰值，count=调研批次"""
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    since = (NOW - timedelta(days=7)).strftime("%Y-%m-%d")
    agg = {}
    for page in (1, 2):
        params = {
            "sortColumns": "NOTICE_DATE,SUM,RECEIVE_START_DATE,SECURITY_CODE",
            "sortTypes": "-1,-1,-1,1", "pageSize": "500", "pageNumber": str(page),
            "reportName": "RPT_ORG_SURVEYNEW", "columns": "ALL",
            "quoteColumns": "f2~01~SECURITY_CODE~CLOSE_PRICE,f3~01~SECURITY_CODE~CHANGE_RATE",
            "source": "WEB", "client": "WEB",
            "filter": f'(NUMBERNEW="1")(IS_SOURCE="1")(NOTICE_DATE>\'{since}\')',
        }
        r = requests.get(url, params=params, headers=UA, timeout=15)
        data = r.json().get("result")
        if not data or not data.get("data"):
            break
        for row in data["data"]:
            code = str(row.get("SECURITY_CODE", "") or "")
            if not code:
                continue
            item = agg.setdefault(code, {
                "code": code, "name": str(row.get("SECURITY_NAME_ABBR", "") or ""),
                "sum": 0, "count": 0, "date": "", "price": None, "pct": None,
            })
            try:
                item["sum"] = max(item["sum"], int(row.get("SUM", 0) or 0))
            except Exception:
                pass
            item["count"] += 1
            d = str(row.get("RECEIVE_START_DATE", "") or "")[:10]
            if d > item["date"]:
                item["date"] = d
            try:
                item["price"] = round(float(row.get("CLOSE_PRICE", 0) or 0), 2)
                item["pct"] = round(float(row.get("CHANGE_RATE", 0) or 0), 2)
            except Exception:
                pass
        if len(data["data"]) < 500:
            break
    result = sorted(agg.values(), key=lambda x: x["sum"], reverse=True)[:8]
    for it in result:
        it["sum"] = int(it["sum"])
    return result


# ---------- 6. 业绩预告（东财） ----------
def collect_earnings():
    year = NOW.year
    result = []
    for cutoff in [f"{year}0630", f"{year}0331", f"{year}0930", f"{year}1231"]:
        if NOW.strftime("%Y%m%d") < cutoff:
            continue
        try:
            df = ak.stock_yjyg_em(date=cutoff)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        seen = set()
        for _, r in df.head(12).iterrows():
            code = str(r.get("股票代码", "") or "")
            if code in seen:
                continue
            seen.add(code)
            chg = str(r.get("业绩变动幅度", "") or "")
            try:
                float(chg)
                chg += "%"
            except ValueError:
                pass
            result.append({
                "code": code,
                "name": str(r.get("股票简称", "") or ""),
                "type": str(r.get("预告类型", "") or ""),
                "change": chg,
                "reason": str(r.get("业绩变动原因", "") or "")[:60],
                "ann_date": str(r.get("公告日期", "") or "")[:10],
            })
        break
    return result[:15]


# ---------- 7. 自选股池行情（腾讯批量接口） ----------
def read_watchlist():
    path = os.path.join(DATA_DIR, "watchlist.txt")
    codes = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    codes.append(line.split(",")[0].strip())
    return codes


def collect_watchlist():
    codes = read_watchlist()
    if not codes:
        return []
    # 行情（腾讯批量，一次请求）
    q = ",".join(tx_code(c) for c in codes)
    r = requests.get("https://qt.gtimg.cn/q=" + q, headers=UA, timeout=15)
    r.encoding = "gbk"
    quotes = {}
    for line in r.text.strip().split(";"):
        if "=" not in line:
            continue
        payload = line.split('="')[1].rstrip('"')
        f = payload.split("~")
        if len(f) < 46:
            continue
        quotes[f[2]] = {
            "code": f[2], "name": f[1].replace(" ", ""),
            "price": round(float(f[3]), 2),
            "pct": round(float(f[32]), 2),
            "chg": round(float(f[31]), 2),
            "turnover": round(float(f[38]), 2),
            "amount": round(float(f[37]) / 10000, 2),
            "pe": round(float(f[39]), 1) if f[39] else None,
            "pb": round(float(f[46]), 2) if len(f) > 46 and f[46] else None,
            "ytd": None,
        }
    # 年初至今涨跌幅（腾讯K线首末收盘）
    ytd_start = f"{NOW.year}0101"
    ytd_end = NOW.strftime("%Y%m%d")
    for code in codes:
        qd = quotes.get(code)
        if qd is None:
            continue
        try:
            kr = requests.get(
                f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={tx_code(code)},day,,,240,qfq",
                headers=UA, timeout=15)
            data = kr.json()["data"][tx_code(code)]
            kline = data.get("qfqday") or data.get("day") or []
            if len(kline) >= 2:
                first_close = float(kline[0][2])
                last_close = float(kline[-1][2])
                qd["ytd"] = round((last_close - first_close) / first_close * 100, 2)
        except Exception:
            pass
        time.sleep(0.15)
    return [q for q in quotes.values() if q]


# ---------- 7. 历史指数K线（腾讯，90日） ----------
def collect_history():
    r = requests.get(
        f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,,,{HIST_DAYS * 2},qfq",
        headers=UA, timeout=15)
    data = r.json()["data"]["sh000001"]
    kline = data.get("qfqday") or data.get("day") or []
    result = [{"date": k[0], "上证指数": round(float(k[2]), 2)} for k in kline]
    return result[-HIST_DAYS:]


def main():
    print(f"=== 股票复盘工作台 · 数据采集 @ {NOW.strftime('%Y-%m-%d %H:%M:%S')} ===")

    index_data = safe(collect_index) or []
    save_json("index_snapshot.json", index_data)

    sector_data = safe(collect_sector) or []
    save_json("sector_rank.json", sector_data)

    econ_data = safe(collect_econ_calendar) or []
    save_json("economic_calendar.json", econ_data)

    unlock_data = safe(collect_unlock) or []
    save_json("unlock_calendar.json", unlock_data)

    sentiment_data = safe(collect_sentiment) or {}
    save_json("sentiment.json", sentiment_data)

    earn_data = safe(collect_earnings) or []
    save_json("earnings.json", earn_data)

    research_data = safe(collect_research) or []
    save_json("research_rank.json", research_data)

    wl_data = safe(collect_watchlist) or []
    save_json("watchlist_quotes.json", wl_data)

    history = safe(collect_history) or []
    save_json("history.json", history)

    save_json("last_update.json", {"time": NOW.strftime("%Y-%m-%d %H:%M:%S")})
    print("=== 采集完成 ===")


if __name__ == "__main__":
    main()
