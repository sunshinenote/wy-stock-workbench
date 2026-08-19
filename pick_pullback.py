# -*- coding: utf-8 -*-
"""
起爆后缩量回调到支撑位 · 选股引擎
========================================
模型：起爆后缩量回调到支撑位（Breakout → Volume-Shrink Pullback to Support）
  1) 起爆：近期(1~20 交易日内)出现过「放量大阳突破」—— 当日涨幅>=BO_GAIN 且
     成交量 >= BO_VOL_MULT × 当日 MA5 量，且当日为区间相对高点。
  2) 缩量：起爆之后成交量持续萎缩——今日量 <= VOL_SHRINK_TODAY × 起爆量，
     且回调期均量 <= VOL_SHRINK_POST × 起爆量，今日量低于近 5 日均量。
  3) 回调：价格从起爆高点回落 PULL_MIN~PULL_MAX（2%~28%），属健康回踩而非反转。
  4) 到支撑：回踩到达某支撑位——MA20 / MA60 / 起爆日收盘价(突破位变支撑) 其中之一
     在容差带内。
  5) 趋势背景：现价 > MA60 且 MA20 > MA60（上升途中的中继，而非下跌反弹）。

标的池（聪明钱 / 内部人已用真金白银表态，五类合并去重）：
  - 机构调研  data/research_rank.json  （sum 调研次数 / count 机构数）
  - 高管增持  data/exec_hold.json      （近半年增持）
  - 私募/公募  data/inst_hold.json      （private=私募, public=公募）
  - 牛散持仓  data/niusan.json          （十大流通股东）

数据源：腾讯行情接口（qt.gtimg.cn / ifzq.gtimg.cn），纯 HTTP，无需 akshare。
依赖：仅 requests + 标准库。

用法：
  python pick_pullback.py [--top 5] [--min-score 55] [--date YYYY-MM-DD] [--max-pool 220]
"""
import json
import os
import sys
import math
import time
import struct
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
OUT = os.path.join(BASE, "deliverables", "pullback")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# 本地通达信日线（最稳，零网络、不触发 WAF）。若缺失则该票回退腾讯接口。
VIPDOC = r"C:\new_tdx\vipdoc"

# ---------------- 模型参数（可调） ----------------
BO_GAIN = 0.05          # 起爆日最小涨幅
BO_VOL_MULT = 1.6       # 起爆日量 / 当日 MA5 量 的最小倍数
BO_WINDOW = 20          # 起爆发生在最近多少个交易日内
PULL_MIN = 0.02         # 最小回调幅度（相对起爆高点）
PULL_MAX = 0.33         # 最大回调幅度（超过视为反转/破位）
VOL_SHRINK_TODAY = 0.75 # 今日量 <= 该比例 × 起爆量 才算缩量
VOL_SHRINK_POST = 0.85  # 回调期均量 <= 该比例 × 起爆量
BAND_MA20 = 0.025       # 贴近 MA20 的容差
BAND_MA60 = 0.030       # 贴近 MA60 的容差
BAND_PIVOT = 0.035      # 贴近起爆突破位的容差
TOP_DEFAULT = 5
MIN_SCORE_DEFAULT = 55


# ---------------- 工具 ----------------
def tx_code(code):
    if code.startswith("6"):
        return "sh" + code
    if code.startswith(("0", "3")):
        return "sz" + code
    return "bj" + code


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


def vol_ma(vols, n=5):
    out = []
    for i in range(len(vols)):
        if i < n - 1:
            out.append(None)
        else:
            out.append(sum(vols[i - n + 1:i + 1]) / n)
    return out


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

    # 机构调研
    try:
        d = json.load(open(os.path.join(DATA, "research_rank.json"), encoding="utf-8"))
        if isinstance(d, list):
            for s in d:
                add(s.get("code"), s.get("name"), "机构调研", float(s.get("count") or 0) * 1.0 + 0.5)
    except Exception as e:
        print("[WARN] research_rank.json 读取失败:", e)
    # 高管增持
    try:
        d = json.load(open(os.path.join(DATA, "exec_hold.json"), encoding="utf-8"))
        for s in d.get("stocks", []):
            add(s.get("code"), s.get("name"), "高管增持", float(s.get("count") or 0) * 0.5)
    except Exception as e:
        print("[WARN] exec_hold.json 读取失败:", e)
    # 私募 / 公募
    try:
        d = json.load(open(os.path.join(DATA, "inst_hold.json"), encoding="utf-8"))
        for s in d.get("private", []):
            add(s.get("code"), s.get("name"), "私募增持", float(s.get("count") or 0) * 0.3)
        for s in d.get("public", []):
            add(s.get("code"), s.get("name"), "公募增持", float(s.get("count") or 0) * 0.3)
    except Exception as e:
        print("[WARN] inst_hold.json 读取失败:", e)
    # 牛散
    try:
        d = json.load(open(os.path.join(DATA, "niusan.json"), encoding="utf-8"))
        for ns in d.get("niusan", []):
            for s in ns.get("stocks", []):
                add(s.get("code"), s.get("name"), "牛散·" + ns.get("name", ""), float(s.get("value") or 0) * 1.0)
    except Exception as e:
        print("[WARN] niusan.json 读取失败:", e)
    return uni


# ---------------- 行情获取 ----------------
def fetch_spot(codes):
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


def fetch_kline_vipdoc(code, want=90):
    """从本地通达信 vipdoc 读取日K（不复权）。返回 (closes,highs,lows,vols) 或 None。"""
    if code.startswith("6"):
        m = "sh"
    elif code.startswith(("0", "3")):
        m = "sz"
    else:
        m = "bj"
    p = os.path.join(VIPDOC, m, "lday", "%s%s.day" % (m, code))
    if not os.path.exists(p):
        return None
    try:
        data = open(p, "rb").read()
    except Exception:
        return None
    n = len(data) // 32
    if n < 60:
        return None
    recs = []
    for i in range(max(0, n - want), n):
        rec = data[i * 32:i * 32 + 32]
        try:
            d, o, h, l, c, amt, vol, res = struct.unpack("<IIIIIfII", rec)
        except Exception:
            continue
        recs.append((c / 100.0, h / 100.0, l / 100.0, vol / 100.0))
    if len(recs) < 60:
        return None
    closes = [r[0] for r in recs]
    highs = [r[1] for r in recs]
    lows = [r[2] for r in recs]
    vols = [r[3] for r in recs]
    return closes, highs, lows, vols


def fetch_kline_tencent(code, retries=3):
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,90,qfq" % tx_code(code)
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=UA, timeout=20)
            data = r.json().get("data", {}).get(tx_code(code), {})
            kline = data.get("qfqday") or data.get("day") or []
            if len(kline) >= 60:
                closes = [float(k[2]) for k in kline]
                highs = [float(k[3]) for k in kline]
                lows = [float(k[4]) for k in kline]
                vols = [float(k[5]) for k in kline]
                return closes, highs, lows, vols
        except Exception:
            pass
        if attempt < retries - 1:
            time.sleep(0.4 * (attempt + 1))
    return None


def fetch_kline(code):
    """优先本地 vipdoc，缺失时回退腾讯接口。"""
    kl = fetch_kline_vipdoc(code)
    if kl:
        return kl
    return fetch_kline_tencent(code)


# ---------------- 核心模型 ----------------
def detect_breakout(closes, highs, vols, vma5):
    """返回 (bo_idx, bo_high, bo_close, bo_vol, bo_gain, bo_volr) 或 None"""
    n = len(closes)
    best = None
    # 起爆发生在 1~BO_WINDOW 个交易日前（不含今日）
    lo = max(1, n - 1 - BO_WINDOW)
    hi = n - 2  # 至少留出 1 天给回调（今日为回踩日）
    for i in range(hi, lo - 1, -1):
        prev = closes[i - 1]
        if prev <= 0:
            continue
        gain = (closes[i] - prev) / prev
        vma = vma5[i]
        volr = (vols[i] / vma) if vma else 0
        if gain >= BO_GAIN and volr >= BO_VOL_MULT:
            # 当日为区间相对高点（高于前后）
            local_high = highs[i] >= max(highs[max(0, i - 3):i + 4])
            score = gain * 2 + volr * 0.1 + (0.3 if local_high else 0)
            if best is None or score > best[0]:
                best = (score, i, highs[i], closes[i], vols[i], gain, volr)
    if best is None:
        return None
    return best[1], best[2], best[3], best[4], best[5], best[6]


def score_pullback(code, kline, name, spot=None):
    closes, highs, lows, vols = kline
    price = closes[-1]          # 用 vipdoc 收盘价，与均线/量能口径一致（不复权）
    if spot:
        name = spot.get("name") or name
        pct = spot.get("pct")
        turnover = spot.get("turnover")
    else:
        pct = (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) > 1 and closes[-2] else 0.0
        turnover = None

    reasons = []
    signals = {}

    # 硬过滤
    if "ST" in name or name.startswith("*"):
        return None, ["ST 股剔除"]
    if pct <= -6.0 or pct > 4.0:
        return None, ["当日涨跌超出回踩区间(pct=%.2f%%)" % pct]
    if turnover is not None and not (1.0 <= turnover <= 18.0):
        return None, ["换手率异常(%.2f%%)" % turnover]

    ma5 = sma(closes, 5)
    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    ma30 = sma(closes, 30)
    ma60 = sma(closes, 60)
    if None in (ma5, ma10, ma20, ma30, ma60):
        return None, ["均线数据不足"]

    # 趋势背景：宽松版——回踩不破 MA20（或恰好在 MA20 上），且未远离 MA60 下方
    # （允许刚起爆、MA20 尚未上穿 MA60 的上升中继形态）
    above_ma20 = price > ma20 * 0.99
    not_far_below_ma60 = price > ma60 * 0.93
    if not (above_ma20 and not_far_below_ma60):
        return None, ["非上升中继(需 现价在MA20上方且未远离MA60)"]
    uptrend = (price > ma60) and (ma20 > ma60)

    vma5 = vol_ma(vols, 5)
    bo = detect_breakout(closes, highs, vols, vma5)
    if bo is None:
        return None, ["近 %d 日无放量大阳起爆" % BO_WINDOW]
    bo_idx, bo_high, bo_close, bo_vol, bo_gain, bo_volr = bo
    days_since = (len(closes) - 1) - bo_idx
    reasons.append("起爆日 %d 天前：涨幅 %.1f%% / 量比 %.1fx" % (days_since, bo_gain * 100, bo_volr))

    # 缩量（今日量按交易时段修正）
    scale = vol_scale()
    today_vol = vols[-1] * scale
    post = vols[bo_idx + 1:]  # 起爆之后到今日
    post_avg = sum(post) / len(post) if post else today_vol
    if today_vol > bo_vol * VOL_SHRINK_TODAY:
        return None, ["今日未缩量(今日量/起爆量=%.2f，需<%.2f)" % (today_vol / bo_vol, VOL_SHRINK_TODAY)]
    if post_avg > bo_vol * VOL_SHRINK_POST:
        return None, ["回调期整体未缩量(均量/起爆量=%.2f)" % (post_avg / bo_vol)]
    if today_vol >= vma5[-1]:
        return None, ["今日量未低于近5日均量"]
    s_vol = clamp((VOL_SHRINK_TODAY - today_vol / bo_vol) / (VOL_SHRINK_TODAY - 0.2))
    signals["缩量"] = round(s_vol * 25, 1)
    reasons.append("缩量：今日量/起爆量=%.2f，回调均量/起爆量=%.2f" % (today_vol / bo_vol, post_avg / bo_vol))

    # 回调幅度
    drawdown = (bo_high - price) / bo_high
    if drawdown < PULL_MIN or drawdown > PULL_MAX:
        return None, ["回调幅度不在区间(%.1f%%，需 %.0f%%~%.0f%%)" % (drawdown * 100, PULL_MIN * 100, PULL_MAX * 100)]
    # 回踩甜区 8%~15% 给满分
    if 0.08 <= drawdown <= 0.15:
        s_pull = 1.0
    elif drawdown < 0.08:
        s_pull = 0.7 + 0.3 * (drawdown - PULL_MIN) / (0.08 - PULL_MIN)
    else:
        s_pull = 0.7 * (1 - (drawdown - 0.15) / (PULL_MAX - 0.15))
    signals["回调"] = round(clamp(s_pull) * 20, 1)
    reasons.append("回调幅度 %.1f%%（起爆高点 %.2f → 现价 %.2f）" % (drawdown * 100, bo_high, price))

    # 到支撑位
    d_ma20 = abs(price - ma20) / ma20 if ma20 else 9
    d_ma60 = abs(price - ma60) / ma60 if ma60 else 9
    d_pivot = abs(price - bo_close) / bo_close if bo_close else 9
    cands = [("MA20", d_ma20, BAND_MA20, 1.0), ("MA60", d_ma60, BAND_MA60, 0.8),
             ("突破位", d_pivot, BAND_PIVOT, 0.9)]
    cands = [c for c in cands if c[1] <= c[2]]
    if not cands:
        return None, ["未回踩到支撑位(MA20/MA60/突破位)"]
    cands.sort(key=lambda x: x[1])
    sup_name, sup_d, sup_band, sup_q = cands[0]
    s_sup = clamp(sup_q * (1 - sup_d / sup_band))
    signals["支撑"] = round(s_sup * 15, 1)
    reasons.append("回踩至支撑：%s（偏离 %.2f%%）" % (sup_name, sup_d * 100))

    # 起爆强度
    s_bo = clamp((bo_gain - BO_GAIN) / (0.10 - BO_GAIN)) * 0.7 + clamp((bo_volr - BO_VOL_MULT) / (3.0 - BO_VOL_MULT)) * 0.3
    signals["起爆"] = round(s_bo * 25, 1)

    # 趋势
    if ma5 > ma10 > ma20 > ma60:
        s_tr = 1.0
        reasons.append("均线完全多头(MA5>MA10>MA20>MA60)")
    elif uptrend:
        s_tr = 0.7
    else:
        s_tr = 0.4
    signals["趋势"] = round(s_tr * 15, 1)

    total = sum(signals.values())
    prob = "高" if total >= 78 else ("中高" if total >= 65 else ("中" if total >= 55 else "低"))
    return {
        "code": code, "name": name, "price": price, "pct": pct, "turnover": turnover,
        "score": round(total, 1), "prob": prob, "signals": signals,
        "reasons": reasons, "drawdown": round(drawdown * 100, 1),
        "bo_days": days_since, "bo_volr": round(bo_volr, 2),
        "ma20": round(ma20, 2), "ma60": round(ma60, 2), "sup": sup_name,
        "series": closes[-60:], "vols": vols[-60:],
    }, None


# ---------------- 输出 ----------------
def build_md(date_str, picks, uni_count):
    lines = []
    lines.append("## 🎯 起爆后缩量回调到支撑位 · %s\n" % date_str)
    lines.append("> 标的池：机构调研 / 高管 / 私募 / 公募 / 牛散 合并去重（共 %d 只）｜ 模型：起爆→缩量→回调→支撑\n" % uni_count)
    if not picks:
        lines.append("\n⚠️ 今日（截至选股时点）标的池中**无**符合「起爆后缩量回调到支撑位」形态的个股。可能原因：标的池普遍未见放量大阳起爆，或起爆后尚未缩量回踩到支撑。\n")
    else:
        lines.append("\n| 排名 | 代码 | 名称 | 现价 | 涨幅 | 换手 | 起爆(天前) | 回调 | 支撑 | 评分 | 概率 |")
        lines.append("|------|------|------|------|------|------|------|------|------|------|------|")
        for i, p in enumerate(picks, 1):
            lines.append("| %d | %s | %s | %.2f | %.2f%% | %.2f%% | %d | %.1f%% | %s | **%.1f** | %s |" % (
                i, p["code"], p["name"], p["price"], p["pct"], p["turnover"],
                p["bo_days"], p["drawdown"], p["sup"], p["score"], p["prob"]))
        lines.append("\n### 各标的信号明细")
        for i, p in enumerate(picks, 1):
            lines.append("\n**%d. %s（%s）评分 %.1f / 概率 %s**" % (i, p["name"], p["code"], p["score"], p["prob"]))
            lines.append("- 内部人信号：" + "、".join(p.get("tags", [])[:8]))
            lines.append("- 现价 %.2f ｜ 涨幅 %.2f%% ｜ 换手 %.2f%% ｜ 起爆 %d 天前(量比%.1fx) ｜ 回调 %.1f%% ｜ 支撑 %s" % (
                p["price"], p["pct"], p["turnover"], p["bo_days"], p["bo_volr"], p["drawdown"], p["sup"]))
            lines.append("- 技术信号：" + "；".join(p["reasons"]))
            lines.append("- 维度分：起爆 %s / 缩量 %s / 回调 %s / 支撑 %s / 趋势 %s" % (
                p["signals"]["起爆"], p["signals"]["缩量"], p["signals"]["回调"],
                p["signals"]["支撑"], p["signals"]["趋势"]))
    lines.append("\n---\n⚠️ 本结果由 AI 根据公开行情与内部人数据，按「起爆后缩量回调到支撑位」模型自动筛选，仅供研究参考，不构成任何投资建议。投资有风险，决策需谨慎。")
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
            <div class="psub">现价 {price} ｜ 涨幅 {pct}% ｜ 换手 {turnover}% ｜ 起爆 {bo}天前(量比{volr}) ｜ 回调 {dd}% ｜ 支撑 {sup}</div></div>
            <div class="badge" style="background:{color}">起爆回调概率 {prob}<br>评分 {score}</div>
          </div>
          <div class="ptags">内部人信号：{tags}</div>
          <div class="preasons">技术信号：{reasons}</div>
        </div>""".format(
            i=i, name=p["name"], code=p["code"], price=p["price"], pct=p["pct"],
            turnover=p["turnover"], bo=p["bo_days"], volr=p["bo_volr"], dd=p["drawdown"],
            sup=p["sup"], color=color, prob=p["prob"], score=p["score"],
            tags="、".join(p.get("tags", [])[:8]), reasons="；".join(p["reasons"]))
    charts = ""
    for i, p in enumerate(picks):
        dates = list(range(len(p["series"])))
        charts += """
        <div class="chart-card">
          <h2>{name}（{code}）近60日走势 + 均线</h2>
          <canvas id="price{i}"></canvas>
        </div>
        <div class="chart-card">
          <h2>{name} 起爆回调维度评分</h2>
          <canvas id="radar{i}"></canvas>
        </div>""".format(name=p["name"], code=p["code"], i=i)
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
          data:{{ labels:['起爆','缩量','回调','支撑','趋势'],
            datasets:[{{data:[{v0},{v1},{v2},{v3},{v4}], fill:true,
              backgroundColor:'rgba(37,99,235,0.2)', borderColor:'#2563eb'}}] }},
          options:{{responsive:true, scales:{{r:{{min:0,max:25}}}}}}
        }});
        """.format(i=i, dates=dates, close=s, ma20=ma20, ma60=ma60,
                   v0=sig["起爆"], v1=sig["缩量"], v2=sig["回调"], v3=sig["支撑"], v4=sig["趋势"])
    if not picks:
        cards = "<p style='padding:24px'>⚠️ 今日（截至选股时点）标的池中无符合「起爆后缩量回调到支撑位」形态的个股。</p>"
    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<title>起爆后缩量回调到支撑位 {date}</title>
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
<h1>🎯 起爆后缩量回调到支撑位</h1>
<div class="meta">日期 {date} ｜ 标的池 {uni} 只（机构调研/高管/私募/公募/牛散）｜ 模型：起爆→缩量→回调→支撑</div>
{cards}
{charts}
<div class="disclaimer">⚠️ 本结果由 AI 根据公开行情与内部人数据，按「起爆后缩量回调到支撑位」模型自动筛选，仅供研究参考，不构成任何投资建议。投资有风险，决策需谨慎。</div>
<script>{js}</script>
</body></html>""".format(date=date_str, uni=uni_count, cards=cards, charts=charts, js=js)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=TOP_DEFAULT)
    ap.add_argument("--min-score", type=float, default=MIN_SCORE_DEFAULT)
    ap.add_argument("--date", type=str, default=datetime.datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--max-pool", type=int, default=220)
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    print("=== 起爆后缩量回调到支撑位 @ %s ===" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    uni = load_universe()
    if not uni:
        print("[ERR] 标的池为空，请先运行 collect.py 生成 data/*.json")
        sys.exit(1)
    ranked = sorted(uni.values(), key=lambda x: -x["sm"])[:args.max_pool]
    codes = [e["code"] for e in ranked]
    print("[pool] 候选池 %d 只（来自总池 %d 只，五类合并）" % (len(codes), len(uni)))

    spot = fetch_spot(codes)
    print("[spot] 获取快照 %d 只" % len(spot))

    results = []
    skipped = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
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
            res, why = score_pullback(code, kl, uni[code]["name"], sp)
            if res:
                res["tags"] = uni[code]["tags"]
                results.append(res)
            else:
                skipped += 1
    print("[score] 命中 %d 只，剔除 %d 只" % (len(results), skipped))

    results.sort(key=lambda x: -x["score"])
    picks = [r for r in results if r["score"] >= args.min_score][:args.top]
    if not picks and results:
        picks = results[:args.top]

    md = build_md(args.date, picks, len(uni))
    html = build_html(args.date, picks, len(uni))
    md_path = os.path.join(OUT, "%s-pullback.md" % args.date)
    html_path = os.path.join(OUT, "%s-pullback.html" % args.date)
    open(md_path, "w", encoding="utf-8").write(md)
    open(html_path, "w", encoding="utf-8").write(html)
    print("[out] %s" % md_path)
    print("[out] %s" % html_path)
    print("\n=== 选出 %d 只 ===" % len(picks))
    for i, p in enumerate(picks, 1):
        print("%d. %s %s  评分%.1f 概率%s  起爆%d天前 回调%.1f%% 支撑%s" % (
            i, p["code"], p["name"], p["score"], p["prob"], p["bo_days"], p["drawdown"], p["sup"]))
    if not picks:
        print("今日无符合模型标的")


if __name__ == "__main__":
    main()
