#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
港股打新数据抓取模块
数据源：阿斯达克新股中心（服务端渲染，用 headless chromium dump-dom 抓取）
  - 即将上市列表: 代码/名称/行业/招股价/每手股数/入场费/截止日/暗盘日/上市日
  - 上市时间表:   招股起止/定价日/公布售股结果日(中签日)/上市日
  - 招股资料:     保荐人/发售结构/市值区间
所有日期均为页面原始披露；抓取失败时返回 error 字段，调用方做降级。
"""

import glob
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

BASE = "https://www.aastocks.com"
LISTING_URL = BASE + "/sc/stocks/market/ipo/upcomingipo/listing.aspx"
TIMETABLE_URL = BASE + "/sc/stocks/market/ipo/upcomingipo/ipo-timetable?symbol={code}&s=3&o=1&s3=1&o3=1"
INFO_URL = BASE + "/sc/stocks/market/ipo/upcomingipo/ipo-info?symbol={code}&s=3&o=1&s3=1&o3=1"
MAX_STOCKS = 6  # 每页抓前 N 只详情，控制耗时


def _chromium() -> str:
    """跨平台查找 chromium：CHROMIUM_PATH 环境变量 → playwright 缓存 → PATH。"""
    env = os.environ.get("CHROMIUM_PATH")
    if env and Path(env).exists():
        return env
    patterns = [
        str(Path.home() / ".cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux/chrome-headless-shell"),
        str(Path.home() / ".cache/ms-playwright/chromium-*/chrome-linux/chrome"),
        "/Users/chenxinghe/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-mac-arm64/chrome-headless-shell",
    ]
    for pat in patterns:
        cands = sorted(glob.glob(pat))
        if cands:
            return cands[-1]
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome"):
        p = shutil.which(name)
        if p:
            return p
    raise RuntimeError("未找到 chromium（设置 CHROMIUM_PATH 或安装 playwright chromium）")


def dump_dom(url: str, timeout: int = 100) -> str:
    last = None
    for _ in range(2):  # 偶发超时/进程被杀，重试一次
        try:
            proc = subprocess.run(
                [_chromium(), "--headless", "--disable-gpu", "--no-sandbox",
                 "--virtual-time-budget=8000", "--dump-dom", url],
                capture_output=True, text=True, timeout=timeout,
            )
            if proc.returncode == 0 and len(proc.stdout) >= 20000:
                return proc.stdout
            last = RuntimeError(f"输出异常(rc={proc.returncode},len={len(proc.stdout)})")
        except subprocess.TimeoutExpired as e:
            last = e
    raise RuntimeError(f"页面抓取失败: {url} ({str(last)[:120]})")


def _text_lines(html: str) -> list[str]:
    t = re.sub(r"<script[\s\S]*?</script>", "", html)
    t = re.sub(r"<style[\s\S]*?</style>", "", t)
    t = re.sub(r"<[^>]+>", "\n", t)
    t = t.replace("&amp;", "&").replace("&nbsp;", " ")
    return [l.strip() for l in t.split("\n") if l.strip()]


def _d(s: str) -> str:
    """'2026/08/07' -> '2026-08-07'，否则空串"""
    m = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})", (s or "").strip())
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else ""


def parse_listing(html: str) -> list[dict]:
    """解析「即将上市」列表表格。页面有多个 tbody，需定位含 .HK 数据行的那个。"""
    stocks = []
    tbody_html = ""
    for tb in re.findall(r"<tbody>([\s\S]*?)</tbody>", html):
        if ".HK" in tb and "symbol=" in tb:
            tbody_html = tb
            break
    if not tbody_html:
        return stocks
    for tr in re.findall(r"<tr[\s\S]*?</tr>", tbody_html):
        tds = re.findall(r"<td[^>]*>([\s\S]*?)</td>", tr)
        if len(tds) < 8:
            continue
        cells = []
        for td in tds:
            txt = re.sub(r"<[^>]+>", " ", td)
            cells.append(re.sub(r"\s+", " ", txt).strip())
        m = re.search(r"symbol=(\d{5})", tr)
        code = m.group(1) if m else ""
        name_m = re.match(r"(.+?)\s+(\d{5})\.HK", cells[1] or "")
        name = name_m.group(1).strip() if name_m else (cells[1] or "")
        stocks.append({
            "code": code,
            "name": name,
            "industry": cells[2],
            "price_range": cells[3],           # 招股价区间（港元）
            "lot_size": cells[4],              # 每手股数
            "entry_fee": cells[5],             # 入场费（港元）
            "apply_end": _d(cells[6]),         # 招股截止日
            "dark_date": _d(cells[7]),         # 暗盘日期
            "listing_date": _d(cells[8]) if len(cells) > 8 else "",
        })
    return [s for s in stocks if s["code"] and s["name"]]


def _after(lines: list[str], label: str) -> str:
    for i, l in enumerate(lines):
        if l == label and i + 1 < len(lines):
            return lines[i + 1]
    return ""


def parse_timetable(html: str) -> dict:
    """上市时间表：招股起止、定价日、公布售股结果日（中签日）。"""
    lines = _text_lines(html)
    out = {"apply_start": "", "apply_end": "", "pricing_date": "", "result_date": "", "listing_date": ""}
    rng = _after(lines, "招股日期")  # '2026/07/30 - 2026/08/04'
    m = re.match(r"(\d{4}/\d{1,2}/\d{1,2})\s*-\s*(\d{4}/\d{1,2}/\d{1,2})", rng)
    if m:
        out["apply_start"], out["apply_end"] = _d(m.group(1)), _d(m.group(2))
    out["pricing_date"] = _d(_after(lines, "定价日期"))
    out["result_date"] = _d(_after(lines, "公布售股结果日期"))
    out["listing_date"] = _d(_after(lines, "上市日期")) or out["listing_date"]
    return out


def parse_ipo_info(html: str) -> dict:
    """招股资料：保荐人、发售结构、市值区间。"""
    lines = _text_lines(html)
    out = {"sponsor": "", "public_offer_lots": None, "mkt_cap": ""}
    out["sponsor"] = _after(lines, "保荐人")
    # 香港/公开发售股数 形如 '5759500(10.00%)'，label 与数值之间可能隔脚注号短行
    for i, l in enumerate(lines):
        if "公开发售股数" in l:
            for j in range(i + 1, min(i + 4, len(lines))):
                m = re.search(r"([\d,]{5,})\s*(?:\(([\d.]+)%\))?", lines[j])
                if m:
                    out["public_offer_shares"] = int(m.group(1).replace(",", ""))
                    out["public_offer_pct"] = m.group(2)
                    break
            break
    for i, l in enumerate(lines):
        if l == "市值" and i + 1 < len(lines):
            out["mkt_cap"] = lines[i + 1]
            break
        if re.match(r"^[\d,]{10,}\s*-\s*[\d,]{10,}$", l):
            out["mkt_cap"] = l
            break
    return out


def detect_conflicts(stocks: list[dict]) -> list[dict]:
    """资金冲突：两只新股的 [申购开始, 公布中签(资金解冻)] 窗口重叠。
    现金申购资金在截止后冻结至公布中签日；融资额度同期占用。"""
    def win(s):
        a = s.get("apply_start") or ""
        b = s.get("result_date") or s.get("listing_date") or s.get("apply_end") or ""
        return (a, b) if a and b else None
    conflicts = []
    for i in range(len(stocks)):
        for j in range(i + 1, len(stocks)):
            w1, w2 = win(stocks[i]), win(stocks[j])
            if not w1 or not w2:
                continue
            lo, hi = max(w1[0], w2[0]), min(w1[1], w2[1])
            if lo <= hi:
                conflicts.append({
                    "pair": [stocks[i]["name"], stocks[j]["name"]],
                    "overlap_start": lo, "overlap_end": hi,
                })
    return conflicts


def fetch_all() -> dict:
    result = {"source": "阿斯达克·新股中心", "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
              "stocks": [], "conflicts": [], "error": ""}
    try:
        stocks = parse_listing(dump_dom(LISTING_URL))
    except Exception as e:
        result["error"] = f"列表抓取失败: {str(e)[:160]}"
        return result
    for s in stocks[:MAX_STOCKS]:
        try:
            s.update({k: v for k, v in parse_timetable(dump_dom(TIMETABLE_URL.format(code=s["code"]))).items() if v})
        except Exception:
            pass
        try:
            s.update({k: v for k, v in parse_ipo_info(dump_dom(INFO_URL.format(code=s["code"]))).items() if v not in (None, "")})
        except Exception:
            pass
        # 公开发售手数 = 公开发售股数 / 每手股数
        try:
            lots = int(str(s.get("lot_size", "")).replace(",", ""))
            if s.get("public_offer_shares") and lots:
                s["public_offer_lots"] = s["public_offer_shares"] // lots
        except ValueError:
            pass
        s.pop("public_offer_shares", None)
    result["stocks"] = stocks
    result["conflicts"] = detect_conflicts(stocks)
    return result


if __name__ == "__main__":
    print(json.dumps(fetch_all(), ensure_ascii=False, indent=2))
