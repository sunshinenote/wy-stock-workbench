# -*- coding: utf-8 -*-
"""
尾盘起爆选股引擎 (每日 14:45 定时运行)
========================================
标的池（聪明钱 / 内部人已用真金白银表态）：
  - 牛散持仓  data/niusan.json     （16 位牛散十大流通股东）
  - 高管增持  data/exec_hold.json   （近半年增持 >=5 次）
  - 私募/公募  data/inst_hold.json   （连续加仓）

选股逻辑（"起爆前夜"模型）：
  - 硬过滤：非 ST、非北交所、温和放量(量比>=1.2)、温和上涨(0<pct<=6.5%)、换手率 1%~15%
  - 打分(0-100)：量能 25 + 均线多头 20 + 位置不高 15 + MACD 15 + KDJ 10 + 突破 15
  - 取分数最高的 1-3 只（>= min_score）作为尾盘起爆候选

数据源：腾讯行情接口（qt.gtimg.cn / ifzq.gtimg.cn），纯 HTTP，无需 akshare。
依赖：仅 requests + 标准库。

用法：
  python pick_breakout.py [--top 3] [--min-score 55] [--date YYYY-MM-DD]
"""
import json
import os
import sys
import math
import time
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
OUT = os.path.join(BASE, "deliverables", "tail-pick")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


# ---------------- 工具 ----------------
def tx_code(code):
    if code.startswith("6"):
        return "sh" + code
    if code.startswith(("0", "3")):
        return "sz" + code
    return "bj" + code  # 北交所，会被硬过滤排除


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def sma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def ema(vals, n):
    if not vals:
        return []
    k = 2.0 / (n + 1)
    e = [vals[0]]
    for v in vals[1:]:
        e.append(v * k + e[-1] * (1 - k))
    return e


def vol_scale():
    """交易时段内对当日 partial 成交量做轻微修正（接近收盘≈1）"""
    now = datetime.datetime.now()
    if now.hour < 9 or (now.hour == 9 and now.minute < 30) or now.hour >= 15:
        return 1.0
    if now.hour < 11 or (now.hour == 11 and now.minute <= 30):
        m = (now.hour - 9) * 60 + now.minute - 30
    elif now.hour >= 13:
        m = 120 + (now.hour - 13) * 60 + now.minute
    else:
        m = 120
    if m <= 0:
        return 1.0
    return min(240.0 / m, 3.0)


# ---------------- 标的池 ----------------
def load_universe():
    uni = {}

    def add(code, name, tag, weight):
        code = str(code).strip()
        if not code or len(code) < 6:
            return
        if code.startswith(("4", "8", "92")):  # 北交所排除
            return
        e = uni.setdefault(code, {"code": code, "name": (name or code), "tags": [], "sm": 0.0})
        if tag not in e["tags"]:
            e["tags"].append(tag)
        e["sm"] += weight

    try:
        d = json.load(open(os.path.join(DATA, "niusan.json"), encoding="utf-8"))
        for ns in d.get("niusan", []):
            for s in ns.get("stocks", []):
                add(s.get("code"), s.get("name"), "牛散·" + ns.get("name", ""), float(s.get("value") or 0) * 1.0)
    except Exception as e:
        print("[WARN] niusan.json 读取失败:", e)
    try:
        d = json.load(open(os.path.join(DATA, "exec_hold.json"), encoding="utf-8"))
        for s in d.get("stocks", []):
            add(s.get("code"), s.get("name"), "高管增持", float(s.get("count") or 0) * 0.5)
    except Exception as e:
        print("[WARN] exec_hold.json 读取失败:", e)
    try:
        d = json.load(open(os.path.join(DATA, "inst_hold.json"), encoding="utf-8"))
        for s in d.get("private", []):
            add(s.get("code"), s.get("name"), "私募增持", float(s.get("count") or 0) * 0.3)
        for s in d.get("public", []):
            add(s.get("code"), s.get("name"), "公募增持", float(s.get("count") or 0) * 0.3)
    except Exception as e:
        print("[WARN] inst_hold.json 读取失败:", e)
    return uni


# ---------------- 行情获取 ----------------
def fetch_spot(codes):
    """批量获取实时快照：price, pct, turnover, name"""
    if not codes:
        return {}
    q = ",".join(tx_code(c) for c in codes)
    try:
        r = requests.get("https://qt.gtimg.cn/q=" + q, headers=UA, timeout=20)
        r.encoding = "gbk"
    except Exception as e:
        print("[WARN] spot 失败:", e)
        return {}
    out = {}
    for line in r.text.strip().split(";"):
        if "=" not in line:
            continue
        payload = line.split('="')[1].rstrip('"')
        f = payload.split("~")
        if len(f) < 46:
            continue
        code = f[2]
        name = f[1].replace(" ", "")
        try:
            price = float(f[3])
            pct = float(f[32])
            turnover = float(f[38])
        except (ValueError, IndexError):
            continue
        out[code] = {"name": name, "price": price, "pct": pct, "turnover": turnover}
    return out


def fetch_kline(code):
    """获取日K：返回 (closes, highs, lows, vols) 或 None"""
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,90,qfq" % tx_code(code)
    try:
        r = requests.get(url, headers=UA, timeout=20)
        data = r.json().get("data", {}).get(tx_code(code), {})
        kline = data.get("qfqday") or data.get("day") or []
    except Exception:
        return None
    if len(kline) < 60:
        return None
    try:
        closes = [float(k[2]) for k in kline]
        highs = [float(k[3]) for k in kline]
        lows = [float(k[4]) for k in kline]
        vols = [float(k[5]) for k in kline]
    except (ValueError, IndexError):
        return None
    return closes, highs, lows, vols


# ---------------- 指标与打分 ----------------
def calc_macd(closes):
    e12 = ema(closes, 12)
    e26 = ema(closes, 26)
    dif = [a - b for a, b in zip(e12, e26)]
    dea = ema(dif, 9)
    hist = [2 * (d - e) for d, e in zip(dif, dea)]
    return dif, dea, hist


def calc_kdj(highs, lows, closes, n=9):
    K, D, J = [], [], []
    pk, pd = 50.0, 50.0
    for i in range(len(closes)):
        if i < n - 1:
            K.append(pk); D.append(pd); J.append(3 * pk - 2 * pd)
            continue
        wh = max(highs[i - n + 1:i + 1])
        wl = min(lows[i - n + 1:i + 1])
        rsv = 50.0 if wh == wl else (closes[i] - wl) / (wh - wl) * 100.0
        k = 2.0 / 3 * pk + 1.0 / 3 * rsv
        d = 2.0 / 3 * pd + 1.0 / 3 * k
        j = 3 * k - 2 * d
        K.append(k); D.append(d); J.append(j)
        pk, pd = k, d
    return K, D, J


def score_stock(code, spot, kline):
    closes, highs, lows, vols = kline
    price = spot["price"]
    pct = spot["pct"]
    turnover = spot["turnover"]
    name = spot["name"]

    reasons = []
    signals = {}

    # 硬过滤
    if "ST" in name or name.startswith("*"):
        return None, ["ST 股剔除"]
    if pct <= 0 or pct > 6.5:
        return None, ["非温和上涨(pct=%.2f%%，要求 0~6.5%%)" % pct]
    if not (1.0 <= turnover <= 15.0):
        return None, ["换手率异常(%.2f%%，要求 1%%~15%%)" % turnover]

    ma5 = sma(closes, 5)
    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    ma60 = sma(closes, 60)
    if None in (ma5, ma10, ma20, ma60):
        return None, ["均线数据不足"]

    # 量能
    scale = vol_scale()
    today_vol = vols[-1] * scale
    prev5 = vols[-6:-1]
    prev_avg = sum(prev5) / len(prev5) if prev5 else today_vol
    vr = today_vol / prev_avg if prev_avg else 0
    if vr < 1.2:
        return None, ["量比不足(%.2f，要求>=1.2)" % vr]
    s_vol = clamp((vr - 1.0) / (2.5 - 1.0))
    signals["量能"] = round(s_vol * 25, 1)
    reasons.append("量比 %.2f（放量）" % vr)

    # 均线多头
    bull_align = (ma5 > ma10 > ma20)
    above_ma20 = price > ma20
    above_ma60 = price > ma60
    if bull_align and above_ma60:
        s_ma = 1.0
    elif above_ma20 and ma5 > ma20:
        s_ma = 0.7
    elif above_ma20:
        s_ma = 0.4
    else:
        s_ma = 0.2
    signals["均线多头"] = round(s_ma * 20, 1)
    if bull_align:
        reasons.append("均线多头排列(MA5>MA10>MA20)")
    elif above_ma20:
        reasons.append("站上 MA20")

    # 位置不高
    win = closes[-60:]
    hi60 = max(win)
    lo60 = min(win)
    pos = (price - lo60) / (hi60 - lo60) if hi60 > lo60 else 0.5
    if 0.45 <= pos <= 0.9 and above_ma60:
        s_pos = 1.0
    elif pos > 0.95:
        s_pos = 0.2
    elif pos < 0.2:
        s_pos = 0.4
    else:
        s_pos = 0.7
    signals["位置不高"] = round(s_pos * 15, 1)
    reasons.append("价格处 60 日区间 %.0f%% 分位（非高位）" % (pos * 100))

    # MACD
    dif, dea, hist = calc_macd(closes)
    cross_up = -999
    for i in range(1, len(dif)):
        if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]:
            cross_up = i
    bars_since = (len(dif) - 1) - cross_up if cross_up > -900 else 999
    bull = dif[-1] > dea[-1]
    rising = dif[-1] > dif[-2] and dea[-1] > dea[-2]
    if 0 <= bars_since <= 8:
        s_macd = 1.0
        reasons.append("MACD 金叉(%d 日内)" % bars_since)
    elif bull and rising:
        s_macd = 0.8
        reasons.append("MACD 多头且向上")
    elif bull:
        s_macd = 0.6
    else:
        s_macd = 0.3
    signals["MACD"] = round(s_macd * 15, 1)

    # KDJ
    K, D, J = calc_kdj(highs, lows, closes)
    kk, dd, jj = K[-1], D[-1], J[-1]
    kd_gold = kk > dd
    if kd_gold and 20 <= kk <= 70 and jj < 95:
        s_kdj = 1.0
        reasons.append("KDJ 金叉后上行(K=%.0f,D=%.0f)" % (kk, dd))
    elif kd_gold:
        s_kdj = 0.7
    elif kk < 30:
        s_kdj = 0.5
        reasons.append("KDJ 低位(K=%.0f)" % kk)
    else:
        s_kdj = 0.3
    signals["KDJ"] = round(s_kdj * 10, 1)

    # 突破/站上
    hi20 = max(closes[-20:])
    near_high = price >= hi20 * 0.97
    if price >= ma5 and near_high:
        s_brk = 1.0
        reasons.append("站上 MA5 且逼近 20 日新高")
    elif price > ma20:
        s_brk = 0.6
    else:
        s_brk = 0.3
    signals["突破"] = round(s_brk * 15, 1)

    total = sum(signals.values())
    prob = "高" if total >= 75 else ("中高" if total >= 60 else ("中" if total >= 50 else "低"))
    return {
        "code": code, "name": name, "price": price, "pct": pct, "turnover": turnover,
        "score": round(total, 1), "prob": prob, "signals": signals,
        "reasons": reasons, "vr": round(vr, 2), "pos": round(pos * 100, 1),
        "ma5": round(ma5, 2), "ma20": round(ma20, 2), "ma60": round(ma60, 2),
        "series": closes[-60:], "vols": vols[-60:],
    }, None


# ---------------- 输出 ----------------
def build_md(date_str, picks, uni_count):
    lines = []
    lines.append("## 📊 尾盘起爆选股 · %s\n" % date_str)
    lines.append("> 标的池：牛散/高管/私募公募增持股（共 %d 只）｜ 模型：起爆前夜技术打分\n" % uni_count)
    if not picks:
        lines.append("\n⚠️ 今日（截至选股时点）无符合「起爆前夜」模型的标的。可能原因：标的池普遍未放量、已涨停或处高位。\n")
    else:
        lines.append("\n| 排名 | 代码 | 名称 | 现价 | 涨幅 | 换手 | 量比 | 起爆评分 | 概率 | 核心信号 |")
        lines.append("|------|------|------|------|------|------|------|----------|------|----------|")
        for i, p in enumerate(picks, 1):
            core = "；".join(p["reasons"][:3])
            lines.append("| %d | %s | %s | %.2f | %.2f%% | %.2f%% | %.2f | **%.1f** | %s | %s |" % (
                i, p["code"], p["name"], p["price"], p["pct"], p["turnover"], p["vr"],
                p["score"], p["prob"], core))
        lines.append("\n### 各标的信号明细")
        for i, p in enumerate(picks, 1):
            lines.append("\n**%d. %s（%s）评分 %.1f / 概率 %s**" % (i, p["name"], p["code"], p["score"], p["prob"]))
            lines.append("- 内部人信号：" + "、".join(p.get("tags", [])[:6]))
            lines.append("- 涨幅 %.2f%% ｜ 换手 %.2f%% ｜ 量比 %.2f ｜ 60日位置 %s%%" % (
                p["pct"], p["turnover"], p["vr"], p["pos"]))
            lines.append("- 技术信号：" + "；".join(p["reasons"]))
            lines.append("- 维度分：量能 %s / 均线 %s / 位置 %s / MACD %s / KDJ %s / 突破 %s" % (
                p["signals"]["量能"], p["signals"]["均线多头"], p["signals"]["位置不高"],
                p["signals"]["MACD"], p["signals"]["KDJ"], p["signals"]["突破"]))
    lines.append("\n---\n⚠️ 本结果由 AI 根据公开行情与内部人增持数据，按技术模型自动筛选，仅供研究参考，不构成任何投资建议。投资有风险，决策需谨慎。")
    return "\n".join(lines)


def build_html(date_str, picks, uni_count):
    cards = ""
    for i, p in enumerate(picks, 1):
        color = "#16a34a" if p["prob"] in ("高", "中高") else ("#d97706" if p["prob"] == "中" else "#64748b")
        cards += """
        <div class="pick">
          <div class="pick-head">
            <span class="rank">#{i}</span>
            <div><div class="pname">{name} <span class="pcode">{code}</span></div>
            <div class="psub">现价 {price} ｜ 涨幅 {pct}% ｜ 换手 {turnover}% ｜ 量比 {vr}</div></div>
            <div class="badge" style="background:{color}">起爆概率 {prob}<br>评分 {score}</div>
          </div>
          <div class="ptags">内部人信号：{tags}</div>
          <div class="preasons">技术信号：{reasons}</div>
        </div>""".format(
            i=i, name=p["name"], code=p["code"], price=p["price"], pct=p["pct"],
            turnover=p["turnover"], vr=p["vr"], color=color, prob=p["prob"], score=p["score"],
            tags="、".join(p.get("tags", [])[:6]), reasons="；".join(p["reasons"]))
    # 图表数据
    charts = ""
    for i, p in enumerate(picks):
        dates = list(range(len(p["series"])))
        charts += """
        <div class="chart-card">
          <h2>{name}（{code}）近60日走势 + 均线</h2>
          <canvas id="price{i}"></canvas>
        </div>
        <div class="chart-card">
          <h2>{name} 起爆维度评分</h2>
          <canvas id="radar{i}"></canvas>
        </div>""".format(name=p["name"], code=p["code"], i=i)
    # JS
    js = ""
    for i, p in enumerate(picks):
        s = p["series"]
        ma20 = [round(sum(s[j - 20:j + 1]) / 20, 2) if j >= 19 else None for j in range(len(s))]
        ma60 = [round(sum(s[j - 60:j + 1]) / 60, 2) if j >= 59 else None for j in range(len(s))]
        sig = p["signals"]
        js += """
        new Chart(document.getElementById('price{i}'), {{
          type:'line',
          data:{{ labels:{dates}, datasets:[
            {{label:'收盘', data:{close}, borderColor:'#2563eb', borderWidth:2, pointRadius:0}},
            {{label:'MA20', data:{ma20}, borderColor:'#f59e0b', borderWidth:1.5, pointRadius:0}},
            {{label:'MA60', data:{ma60}, borderColor:'#9ca3af', borderWidth:1.5, pointRadius:0}}
          ]}},
          options:{{responsive:true, plugins:{{legend:{{labels:{{font:{{size:12}}}}}}}}}}
        }});
        new Chart(document.getElementById('radar{i}'), {{
          type:'radar',
          data:{{ labels:['量能','均线多头','位置不高','MACD','KDJ','突破'],
            datasets:[{{data:[{v0},{v1},{v2},{v3},{v4},{v5}], fill:true,
              backgroundColor:'rgba(37,99,235,0.2)', borderColor:'#2563eb'}}] }},
          options:{{responsive:true, scales:{{r:{{min:0,max:25}}}}}}
        }});
        """.format(i=i, dates=dates, close=s, ma20=ma20, ma60=ma60,
                   v0=sig["量能"], v1=sig["均线多头"], v2=sig["位置不高"],
                   v3=sig["MACD"], v4=sig["KDJ"], v5=sig["突破"])
    if not picks:
        cards = "<p style='padding:24px'>⚠️ 今日无符合「起爆前夜」模型的标的。</p>"
    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<title>尾盘起爆选股 {date}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
 body{{font-family:-apple-system,'PingFang SC',sans-serif;max-width:980px;margin:0 auto;padding:24px;background:#f7f8fa;color:#1f2937}}
 h1{{font-size:24px;margin:0 0 4px}}
 .meta{{color:#6b7280;font-size:13px;margin-bottom:16px}}
 .pick{{background:#fff;border-radius:12px;padding:16px;margin-bottom:14px;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
 .pick-head{{display:flex;align-items:center;gap:14px}}
 .rank{{font-size:28px;font-weight:700;color:#2563eb;min-width:42px}}
 .pname{{font-size:18px;font-weight:600}}
 .pcode{{font-size:13px;color:#9ca3af;font-weight:400}}
 .psub{{font-size:13px;color:#6b7280;margin-top:2px}}
 .badge{{margin-left:auto;text-align:center;color:#fff;border-radius:10px;padding:10px 14px;font-weight:600;font-size:13px;line-height:1.4}}
 .preasons{{font-size:13px;color:#374151;margin-top:10px;background:#f1f5f9;border-radius:8px;padding:10px}}
 .ptags{{font-size:12px;color:#7c3aed;margin-top:8px}}
 .chart-card{{background:#fff;border-radius:12px;padding:20px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
 .chart-card h2{{margin-top:0;font-size:16px}}
 .disclaimer{{color:#9ca3af;font-size:12px;padding:16px 0;border-top:1px solid #e5e7eb;margin-top:16px}}
</style></head><body>
<h1>📊 尾盘起爆选股</h1>
<div class="meta">日期 {date} ｜ 标的池 {uni} 只（牛散/高管/私募公募增持）｜ 模型：起爆前夜技术打分</div>
{cards}
{charts}
<div class="disclaimer">⚠️ 本结果由 AI 根据公开行情与内部人增持数据，按技术模型自动筛选，仅供研究参考，不构成任何投资建议。投资有风险，决策需谨慎。</div>
<script>{js}</script>
</body></html>""".format(date=date_str, uni=uni_count, cards=cards, charts=charts, js=js)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--min-score", type=float, default=55)
    ap.add_argument("--date", type=str, default=datetime.datetime.now().strftime("%Y-%m-%d"))
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    print("=== 尾盘起爆选股 @ %s ===" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    uni = load_universe()
    if not uni:
        print("[ERR] 标的池为空，请先运行 collect.py 生成 data/niusan.json 等")
        sys.exit(1)
    # 按 smart_money 优先级截断，控制请求量
    ranked = sorted(uni.values(), key=lambda x: -x["sm"])
    ranked = ranked[:150]
    codes = [e["code"] for e in ranked]
    print("[pool] 候选池 %d 只（来自总池 %d 只）" % (len(codes), len(uni)))

    spot = fetch_spot(codes)
    print("[spot] 获取快照 %d 只" % len(spot))

    results = []
    skipped = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        fut = {ex.submit(fetch_kline, c): c for c in codes}
        for f in as_completed(fut):
            code = fut[f]
            kl = f.result()
            if not kl:
                skipped += 1
                continue
            sp = spot.get(code)
            if not sp:
                continue
            res, why = score_stock(code, sp, kl)
            if res:
                res["tags"] = uni[code]["tags"]
                results.append(res)
            else:
                skipped += 1
    print("[score] 通过硬过滤 %d 只，剔除 %d 只" % (len(results), skipped))

    results.sort(key=lambda x: -x["score"])
    picks = [r for r in results if r["score"] >= args.min_score][:args.top]
    if not picks and results:  # 兜底：若全低于阈值但有过滤票，取前 top
        picks = results[:args.top]

    md = build_md(args.date, picks, len(uni))
    html = build_html(args.date, picks, len(uni))
    md_path = os.path.join(OUT, "%s-picks.md" % args.date)
    html_path = os.path.join(OUT, "%s-picks.html" % args.date)
    open(md_path, "w", encoding="utf-8").write(md)
    open(html_path, "w", encoding="utf-8").write(html)
    print("[out] %s" % md_path)
    print("[out] %s" % html_path)
    print("\n=== 选出 %d 只 ===" % len(picks))
    for i, p in enumerate(picks, 1):
        print("%d. %s %s  评分%.1f 概率%s  涨幅%.2f%% 量比%.2f" % (
            i, p["code"], p["name"], p["score"], p["prob"], p["pct"], p["vr"]))
    if not picks:
        print("今日无符合模型标的")


if __name__ == "__main__":
    main()
