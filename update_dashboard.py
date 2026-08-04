#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
投资策略看板 · 每日更新脚本
从腾讯文档「日更数据」拉取 5 个策略工作表，生成单文件 HTML 可视化看板。

用法:
    python3 update_dashboard.py

数据源: https://docs.qq.com/sheet/DWmlnRURUbnNHd1Zu (file_id: ZigEDTnsGwVn)
包含: 转债到期为正摊大饼 / 转债低溢价轮动 / 小市值轮动 / 股债ETF轮动 / QDII基金池
排除: 债券指数基金池(zvsusv) / 中资ETF基金池(gegt8w)   ← 用户明确要求排除
"""

import csv
import io
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

# ---------- 配置 ----------
PYTHON = "/Users/chenxinghe/.workbuddy/binaries/python/versions/3.13.12/bin/python3"
NODE = "/Users/chenxinghe/.workbuddy/binaries/node/versions/22.22.2/bin/node"
TDOC_CLI = "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/resources/builtin-plugins/tencent-docs-plugin/skills/tencent-docs/tencentdocs.py"
WESTOCK_CLI = "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/resources/builtin-skills/westock-data/scripts/index.js"
FILE_ID = "ZigEDTnsGwVn"
DOC_URL = "https://docs.qq.com/sheet/DWmlnRURUbnNHd1Zu"
# 可转债打新数据源：东方财富可转债列表（公开接口）
CB_IPO_API = "https://datacenter-web.eastmoney.com/api/data/v1/get"

WORKDIR = Path(__file__).resolve().parent
OUT_HTML = WORKDIR / "index.html"
DEPLOY_HTML = WORKDIR / "deploy" / "index.html"   # 部署目录副本（PWA 静态站）
OUT_JSON = WORKDIR / "data" / "dashboard_data.json"
# 云端模式（GitHub Actions）：用上次发布的数据做腾讯文档失败时的降级基底
FALLBACK_JSON = WORKDIR / "deploy" / "dashboard_data.json"

# (sheet_id, 名称, 行数, 列数)
SHEETS = [
    ("BB08J2", "转债到期为正摊大饼", 200, 14),
    ("BB08J3", "转债低溢价轮动", 201, 15),
    ("BB08J4", "小市值轮动", 201, 28),
    ("BB08J5", "股债ETF轮动", 196, 6),
    ("7o3245", "QDII基金池", 201, 17),
]

SHEET_MCP_URL = "https://docs.qq.com/api/v6/sheet/mcp"


# ---------- 数据拉取 ----------
def _call_sheet_mcp_cloud(tool: str, arguments: dict) -> dict:
    """云端模式：直接 HTTP 调 sheet-mcp（token 来自环境变量 TDOC_OAUTH_ACCESS_TOKEN /
    TDOC_ONEID_ACCESS_TOKEN，由 GitHub Secrets 注入）。兼容 SSE 响应。"""
    oauth = os.environ.get("TDOC_OAUTH_ACCESS_TOKEN", "")
    oneid = os.environ.get("TDOC_ONEID_ACCESS_TOKEN", "")
    if not (oauth or oneid):
        raise RuntimeError("云端模式缺少 TDOC token（Secrets 未配置或已过期）")
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if oauth:
        headers["Authorization"] = f"Bearer {oauth}"
    if oneid:
        headers["X-Oneid-Access-Token"] = oneid
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": tool, "arguments": arguments}}
    req = urllib.request.Request(SHEET_MCP_URL, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            ctype = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:200]
        raise RuntimeError(f"sheet-mcp HTTP {e.code}: {detail}")
    if "text/event-stream" in ctype:
        lines = [l[5:].strip() for l in raw.splitlines() if l.startswith("data:")]
        raw = next((c for c in reversed(lines) if c.startswith("{")), raw)
    return json.loads(raw)


def fetch_sheet(sheet_id: str, rows: int, cols: int) -> list[list[str]]:
    """通过 sheet-mcp get_cell_data 拉取一个子表，返回二维网格。
    本地走 tencentdocs.py CLI；云端（无 CLI 文件）走内置 HTTP 调用。"""
    args = {
        "file_id": FILE_ID,
        "sheet_id": sheet_id,
        "start_row": 0,
        "start_col": 0,
        "end_row": rows - 1,
        "end_col": cols - 1,
        "return_csv": True,
    }
    if Path(TDOC_CLI).exists():
        proc = subprocess.run(
            [PYTHON, TDOC_CLI, "tdoc_call", "sheet-mcp", "get_cell_data", json.dumps(args, ensure_ascii=False)],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"拉取子表 {sheet_id} 失败: {proc.stderr[:300]}")
        resp = json.loads(proc.stdout)
    else:
        resp = _call_sheet_mcp_cloud("get_cell_data", args)
    if "error" in resp:
        raise RuntimeError(f"子表 {sheet_id} 返回错误: {resp['error']}")
    csv_text = resp["result"]["structuredContent"]["csv_data"]
    grid = [row for row in csv.reader(io.StringIO(csv_text))]
    # 裁剪掉全空尾行
    while grid and not any(c.strip() for c in grid[-1]):
        grid.pop()
    return grid


def cell(grid, r, c):
    if r < len(grid) and c < len(grid[r]):
        return grid[r][c].strip()
    return ""


def extract_date(text: str) -> str:
    m = re.search(r"(\d{4}/\d{1,2}/\d{1,2})", text or "")
    return m.group(1) if m else ""


def grid_date(grid, max_rows=5) -> str:
    """在前几行的所有单元格里找数据使用期限日期。"""
    for r in range(min(max_rows, len(grid))):
        for c in grid[r]:
            d = extract_date(c)
            if d:
                return d
    return ""


def find_header_row(grid, keyword: str) -> int:
    """找到包含指定表头关键字（如 编号 / 基金代码）的行号，找不到返回 -1。"""
    for r, row in enumerate(grid):
        if keyword in [c.strip() for c in row]:
            return r
    return -1


# ---------- 各表解析 ----------
def parse_rotation(grid, code_label):
    """解析「保守版/激进版」双池结构（低溢价转债、小市值共用布局）。
    列布局: 1-3 保守1-10, 4-6 保守11-20, 8-10 激进1-10, 11-13 激进11-20
    """
    data_date = grid_date(grid)
    hdr = find_header_row(grid, "编号")
    start = hdr + 1 if hdr >= 0 else 2
    pools = {"保守版": [], "激进版": []}
    groups = [("保守版", [(1, 2, 3), (4, 5, 6)]), ("激进版", [(8, 9, 10), (11, 12, 13)])]
    for pool_name, col_groups in groups:
        for nc, cc, mc in col_groups:
            for r in range(start, len(grid)):
                num, code, name = cell(grid, r, nc), cell(grid, r, cc), cell(grid, r, mc)
                if not (num or code or name):
                    continue
                if not num.isdigit():  # 说明行(如剔除规则)跳过
                    continue
                if code or name:
                    pools[pool_name].append({"num": int(num), "code": code, "name": name})
    # 剔除规则等备注（数据区非编号文本）
    notes = []
    for r in range(start, len(grid)):
        v = cell(grid, r, 1)
        if v and not v.isdigit():
            notes.append(v)
    return {"date": data_date, "pools": pools, "notes": notes, "code_label": code_label}


def parse_tandabing(grid):
    """摊大饼: 4 组 (编号/代码/名称)，最多 100 只，可能空仓。"""
    data_date = grid_date(grid)
    hdr = find_header_row(grid, "编号")
    start = hdr + 1 if hdr >= 0 else 2
    holdings = []
    for nc, cc, mc in [(1, 2, 3), (4, 5, 6), (7, 8, 9), (10, 11, 12)]:
        for r in range(start, len(grid)):
            num, code, name = cell(grid, r, nc), cell(grid, r, cc), cell(grid, r, mc)
            if num.isdigit() and (code or name):
                holdings.append({"num": int(num), "code": code, "name": name})
    holdings.sort(key=lambda x: x["num"])
    return {"date": data_date, "holdings": holdings, "capacity": 100}


def parse_etf_rotation(grid):
    """股债ETF轮动: 当前持仓 + 近22日涨幅/BIAS 列表。"""
    data_date = grid_date(grid)
    holdings = []
    perf_start = -1
    for r, row in enumerate(grid):
        line = cell(grid, r, 1)
        m = re.match(r"(国内版|国际版)[:：](.+?)（(.+?)）\s*已持续(\d+)个交易日", line)
        if m:
            holdings.append({"edition": m.group(1), "name": m.group(2).strip(),
                             "code": m.group(3).strip(), "days": int(m.group(4))})
        if "近22个交易日涨幅" in line or "近22个交易日涨幅" in "".join(row):
            perf_start = r + 1
    perf = []
    for r in range(perf_start if perf_start > 0 else len(grid), len(grid)):
        label = cell(grid, r, 1)
        if not label:
            continue
        m = re.match(r"(.+?)（(.+?)）", label)
        name, code = (m.group(1).strip(), m.group(2).strip()) if m else (label, "")
        chg_raw = cell(grid, r, 3).replace("%", "").strip()
        bias_raw = cell(grid, r, 4).strip()
        try:
            chg = float(chg_raw)
        except ValueError:
            chg = None
        try:
            bias = float(bias_raw)
        except ValueError:
            bias = None
        perf.append({"name": name, "code": code, "chg22": chg, "bias": bias})
    return {"date": data_date, "holdings": holdings, "perf": perf}


def parse_qdii(grid):
    """QDII基金池: 分类向下填充，含限购状态/金额/费率。"""
    data_date = grid_date(grid)
    title = ""
    for row in grid[:5]:
        joined = "".join(row)
        if "QDII" in joined:
            title = next((c.strip() for c in row if "QDII" in c), "")
            break
    hdr = find_header_row(grid, "基金代码")
    start = hdr + 1 if hdr >= 0 else 2
    funds = []
    category = ""
    for r in range(start, len(grid)):
        cat = cell(grid, r, 1)
        if cat:
            category = cat
        code = cell(grid, r, 2)
        name = cell(grid, r, 3)
        if not (code or name) or code == "基金代码":
            continue
        funds.append({
            "category": category,
            "code": code,
            "name": name,
            "status": cell(grid, r, 4),           # 限购 / 暂停申购 / 不限购
            "limit": cell(grid, r, 5),            # 限购金额(元/日)，空=不限
            "mgmt_fee": cell(grid, r, 6),
            "custody_fee": cell(grid, r, 7),
            "sales_fee": cell(grid, r, 8),
            "duration": cell(grid, r, 10),        # 美债久期备注
        })
    categories = []
    for f in funds:
        if f["category"] not in categories:
            categories.append(f["category"])
    return {"date": data_date, "title": title, "funds": funds, "categories": categories}


# ---------- 打新数据 ----------
def _d10(s):
    """'2026-08-04 00:00:00' -> date(2026,8,4)，失败返回 None"""
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def fetch_cb_ipo():
    """可转债打新：东财转债列表中筛「待申购 + 待上市」。
    数据源: 东方财富 RPT_BOND_CB_LIST。失败时返回空结构，看板降级显示。"""
    result = {"source": "东方财富·可转债列表", "apply": [], "listing": [], "error": ""}
    try:
        params = {
            "reportName": "RPT_BOND_CB_LIST",
            "columns": "SECURITY_CODE,SECURITY_NAME_ABBR,CONVERT_STOCK_CODE,"
                       "SECURITY_SHORT_NAME,PUBLIC_START_DATE,LISTING_DATE,"
                       "ACTUAL_ISSUE_SCALE,RATING,INITIAL_TRANSFER_PRICE,CORRECODE",
            "pageSize": "200", "pageNumber": "1",
            "sortColumns": "PUBLIC_START_DATE", "sortTypes": "-1",
            "source": "WEB", "client": "WEB",
        }
        url = CB_IPO_API + "?" + urllib.parse.urlencode(params)
        payload = None
        last_err = None
        for _ in range(3):  # 东财接口偶发 SSL 超时，重试 3 次
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                break
            except Exception as e:
                last_err = e
        if payload is None:
            raise RuntimeError(f"东财接口 3 次重试均失败: {last_err}")
        rows = (payload.get("result") or {}).get("data") or []
        today = date.today()
        for row in rows:
            apply_d = _d10(row.get("PUBLIC_START_DATE"))
            list_d = _d10(row.get("LISTING_DATE"))
            item = {
                "code": row.get("SECURITY_CODE") or "",
                "name": row.get("SECURITY_NAME_ABBR") or "",
                "apply_code": row.get("CORRECODE") or "",
                "stock_name": row.get("SECURITY_SHORT_NAME") or "",
                "stock_code": row.get("CONVERT_STOCK_CODE") or "",
                "apply_date": str(row.get("PUBLIC_START_DATE") or "")[:10],
                "listing_date": str(row.get("LISTING_DATE") or "")[:10],
                "scale": row.get("ACTUAL_ISSUE_SCALE"),
                "rating": row.get("RATING") or "",
                "transfer_price": row.get("INITIAL_TRANSFER_PRICE"),
            }
            if apply_d and apply_d >= today:
                item["status"] = "today" if apply_d == today else "apply"
                result["apply"].append(item)
            elif apply_d and apply_d < today and (list_d is None or list_d >= today):
                item["status"] = "listing"
                result["listing"].append(item)
        result["apply"].sort(key=lambda x: x["apply_date"])
        result["listing"].sort(key=lambda x: x["listing_date"] or "9999")
    except Exception as e:  # 数据源故障时看板其余部分不受影响
        result["error"] = str(e)[:200]
    return result


def fetch_hk_ipo():
    """港股打新：主源阿斯达克新股中心（hk_ipo 模块，字段最全），
    备用腾讯自选股 westock CLI（仅基础字段）。失败返回空结构。"""
    try:
        sys.path.insert(0, str(WORKDIR))
        import hk_ipo
        full = hk_ipo.fetch_all()
        if full["stocks"]:
            full["via"] = "aastocks"
            return full
    except Exception as e:
        full = {"source": "阿斯达克·新股中心", "stocks": [], "conflicts": [],
                "error": f"主源异常: {str(e)[:120]}", "fetched_at": ""}
    # 备用：westock（字段少，结构对齐）；云端无此 CLI 时跳过
    if not Path(WESTOCK_CLI).exists():
        full["error"] = (full.get("error", "") + "; 备用源在此环境不可用").strip("; ")
        return full
    try:
        proc = subprocess.run(
            [NODE, WESTOCK_CLI, "ipo", "--market", "hk", "--raw"],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr[:200] or f"exit {proc.returncode}")
        stocks = []
        for it in (json.loads(proc.stdout) or []):  # 无新股时接口可能返回 null
            ap = (it.get("sgrq") or "").split("~")
            stocks.append({
                "code": (it.get("code") or "").zfill(5), "name": it.get("name") or "",
                "industry": it.get("hy") or "", "price_range": (it.get("price") or "").replace("~", "-"),
                "lot_size": "", "entry_fee": "",
                "apply_start": ap[0] if len(ap) > 1 else "",
                "apply_end": ap[-1] if ap else "",
                "result_date": "", "dark_date": "", "pricing_date": "",
                "listing_date": it.get("ssrq") or "", "stage": it.get("stage") or "",
                "sponsor": "", "mkt_cap": "",
            })
        return {"source": "腾讯自选股·新股日历(备用)", "via": "westock",
                "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "stocks": stocks, "conflicts": [], "error": full.get("error", "")}
    except Exception as e:
        full["error"] = (full.get("error", "") + f"; 备用源异常: {str(e)[:120]}").strip("; ")
        return full


# ---------- 汇总洞察 ----------
def build_insights(data):
    insights = []
    lp = data["low_premium"]["pools"]
    overlap_lp = {x["code"] for x in lp["保守版"]} & {x["code"] for x in lp["激进版"]}
    if overlap_lp:
        names = [x["name"] for x in lp["保守版"] if x["code"] in overlap_lp]
        insights.append(f"转债低溢价轮动 保守/激进双版重合 {len(overlap_lp)} 只：{'、'.join(names)}")
    sc = data["small_cap"]["pools"]
    overlap_sc = {x["code"] for x in sc["保守版"]} & {x["code"] for x in sc["激进版"]}
    if overlap_sc:
        names = [x["name"] for x in sc["保守版"] if x["code"] in overlap_sc]
        insights.append(f"小市值轮动 保守/激进双版重合 {len(overlap_sc)} 只：{'、'.join(names)}")
    # 转债池与正股池联动（转债代码 6 位数字 vs 股票代码前 6 位无法直接对应，按名称匹配）
    bond_names = {x["name"].replace("转债", "") for p in lp.values() for x in p}
    stock_hits = [x["name"] for p in sc.values() for x in p if x["name"] in bond_names]
    if stock_hits:
        insights.append(f"跨策略联动：{'、'.join(sorted(set(stock_hits)))} 的正股同时出现在转债池与股票池")
    etf = data["etf_rotation"]
    for h in etf["holdings"]:
        if h.get("days") is not None:
            insights.append(f"股债ETF轮动{h['edition']}当前持有 {h['name']}，已持续 {h['days']} 个交易日")
    if not data["tandabing"]["holdings"]:
        insights.append("转债到期为正摊大饼策略当前空仓，100 个持仓位待填充")
    qdii = data["qdii"]["funds"]
    paused = sum(1 for f in qdii if "暂停" in f["status"])
    free = sum(1 for f in qdii if "不限购" in f["status"])
    insights.append(f"QDII基金池 {len(qdii)} 只基金：暂停申购 {paused} 只，不限购 {free} 只，其余限购")
    # 打新提醒
    cb = data.get("cb_ipo", {})
    for it in cb.get("apply", []):
        tag = "今日申购" if it.get("status") == "today" else f"{it['apply_date']} 申购"
        insights.append(f"可转债打新：{it['name']}（正股 {it['stock_name']}，{it.get('scale') or '?'} 亿，{it.get('rating') or '未评级'}）{tag}")
    for it in cb.get("listing", [])[:3]:
        ld = it.get("listing_date") or "日期待定"
        insights.append(f"转债待上市：{it['name']} 上市日 {ld}")
    hk = data.get("hk_ipo", {})
    for it in hk.get("stocks", [])[:5]:
        stage_txt = it.get("stage") or ("招股中" if it.get("apply_start") else "待上市")
        insights.append(f"港股打新：{it['name']}（{it['code']}）{stage_txt}，招股价 {it.get('price_range','?')} 港元，{it.get('listing_date','?')} 上市")
    for c in hk.get("conflicts", []):
        insights.append(f"⚠️ 资金冲突：{c['pair'][0]} 与 {c['pair'][1]} 申购资金冻结期重叠（{c['overlap_start']} ~ {c['overlap_end']}），需统筹安排")
    return insights


# ---------- HTML 生成 ----------
def render_html(data: dict) -> str:
    template = HTML_TEMPLATE
    payload = json.dumps(data, ensure_ascii=False)
    return template.replace("__DATA_JSON__", payload)


# HTML 模板（零外部依赖，涨红跌绿，浅色主题）
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>投资策略看板 · 日更数据</title>
<link rel="manifest" href="manifest.webmanifest">
<meta name="theme-color" content="#2f5d50">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="策略看板">
<link rel="apple-touch-icon" href="icons/icon-180.png">
<link rel="icon" type="image/png" sizes="192x192" href="icons/icon-192.png">
<style>
:root{
  --bg:#f6f4ef; --card:#ffffff; --ink:#2b2b2b; --ink2:#6b6b6b; --line:#e8e4da;
  --up:#d43d2a; --down:#1e9e6a; --accent:#2f5d50; --accent-soft:#e7efec;
  --gold:#b8860b; --chip:#f0ede5;
}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;line-height:1.6;padding-bottom:60px}
.wrap{max-width:1180px;margin:0 auto;padding:0 20px}
header.top{padding:34px 0 10px;border-bottom:1px solid var(--line);margin-bottom:22px}
header.top h1{font-size:26px;letter-spacing:1px}
header.top h1 .dot{color:var(--accent)}
.meta{color:var(--ink2);font-size:13px;margin-top:8px;display:flex;gap:18px;flex-wrap:wrap}
.meta b{color:var(--accent);font-weight:600}
nav.tabs{position:sticky;top:0;z-index:50;background:rgba(246,244,239,.94);backdrop-filter:blur(6px);padding:10px 0;border-bottom:1px solid var(--line);margin-bottom:24px;display:flex;gap:8px;flex-wrap:wrap}
nav.tabs a{font-size:13px;color:var(--ink2);text-decoration:none;padding:5px 12px;border-radius:16px;background:var(--chip);transition:.2s}
nav.tabs a:hover{color:#fff;background:var(--accent)}
section{margin-bottom:34px;scroll-margin-top:64px}
h2{font-size:18px;margin-bottom:14px;display:flex;align-items:center;gap:10px}
h2 .bar{width:4px;height:18px;background:var(--accent);border-radius:2px;display:inline-block}
h2 .sub{font-size:12px;color:var(--ink2);font-weight:400}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;box-shadow:0 1px 3px rgba(0,0,0,.04)}
.card .t{font-size:12px;color:var(--ink2);margin-bottom:6px}
.card .v{font-size:22px;font-weight:700;color:var(--accent)}
.card .v small{font-size:12px;font-weight:400;color:var(--ink2);margin-left:4px}
.card .d{font-size:12px;color:var(--ink2);margin-top:4px}
.card.empty .v{color:var(--ink2)}
.insight{background:#fbf7ec;border:1px solid #eadfc3;border-radius:12px;padding:14px 18px}
.insight li{font-size:13px;margin-left:18px;color:#5c4f2e}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:18px}
@media(max-width:860px){.grid2{grid-template-columns:1fr}}
/* 移动端细化 */
@media(max-width:640px){
  body{font-size:14px}
  .wrap{padding:0 14px}
  header.top{padding:22px 0 8px}
  header.top h1{font-size:20px}
  .meta{font-size:12px;gap:10px}
  nav.tabs{flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch;padding:8px 0}
  nav.tabs a{white-space:nowrap;flex-shrink:0}
  h2{font-size:16px;flex-wrap:wrap}
  .cards{grid-template-columns:1fr 1fr;gap:10px}
  .card{padding:12px 14px}
  .card .v{font-size:18px}
  .panel{padding:14px}
  table{font-size:12px}
  th,td{padding:6px 7px}
  .barrow{grid-template-columns:92px 1fr 62px;font-size:12px}
  .holdcard{flex-direction:column}
  .hold{min-width:0}
  .insight li{font-size:12px}
  section{margin-bottom:26px}
}
@media(max-width:380px){
  .cards{grid-template-columns:1fr}
}
.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px}
.panel h3{font-size:14px;margin-bottom:10px;display:flex;justify-content:space-between;align-items:center}
.badge{font-size:11px;padding:2px 9px;border-radius:11px;font-weight:400}
.badge.cons{background:#e8f0ee;color:var(--accent)}
.badge.aggr{background:#fdeeea;color:var(--up)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{color:var(--ink2);font-weight:500;text-align:left;padding:7px 10px;border-bottom:2px solid var(--line);white-space:nowrap}
td{padding:6px 10px;border-bottom:1px solid #f0ede6}
tr:hover td{background:#faf9f5}
tr.dup td{background:#fdf3e7}
tr.dup:hover td{background:#fbe9d5}
.code{font-family:"SF Mono",Menlo,Consolas,monospace;font-size:12px;color:var(--ink2)}
.tag{display:inline-block;font-size:11px;padding:1px 8px;border-radius:10px;white-space:nowrap}
.tag.pause{background:#f3e8e6;color:#8c4a3c}
.tag.limit{background:#fdf3e0;color:#9a6b1a}
.tag.free{background:#e6f4ec;color:#177a52}
.tag.hot{background:#fbe2de;color:#c13a26;font-weight:600}
.tag.soon{background:#e3ecf7;color:#2b5a8a}
.holdcard{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:16px}
.hold{flex:1;min-width:240px;background:linear-gradient(135deg,#2f5d50,#3a7061);color:#fff;border-radius:12px;padding:16px 18px}
.hold.intl{background:linear-gradient(135deg,#7a5c2e,#94703c)}
.hold .ed{font-size:12px;opacity:.85}
.hold .nm{font-size:18px;font-weight:700;margin:4px 0}
.hold .dd{font-size:12px;opacity:.85}
.barwrap{margin:10px 0}
.barrow{display:grid;grid-template-columns:180px 1fr 74px;gap:10px;align-items:center;padding:5px 0;font-size:13px}
.barrow .lbl{text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bartrack{position:relative;height:20px;background:#f1eee6;border-radius:4px;overflow:hidden}
.barzero{position:absolute;left:50%;top:0;bottom:0;width:1px;background:#cfc9ba}
.barfill{position:absolute;top:2px;bottom:2px;border-radius:3px}
.barval{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.chip{font-size:12px;padding:4px 13px;border-radius:14px;background:var(--chip);color:var(--ink2);cursor:pointer;border:1px solid transparent;user-select:none}
.chip.on{background:var(--accent);color:#fff}
.note{font-size:12px;color:var(--ink2);margin-top:10px}
footer{margin-top:44px;padding-top:18px;border-top:1px solid var(--line);color:var(--ink2);font-size:12px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:10px}
footer a{color:var(--accent)}
.legend{font-size:11px;color:var(--ink2);margin-left:8px}
.dotup,.dotdown{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px}
.dotup{background:var(--up)}.dotdown{background:var(--down)}
.empty-state{text-align:center;padding:34px 10px;color:var(--ink2)}
.empty-state .big{font-size:40px;margin-bottom:8px}
/* 打新日历甘特 */
.gantt{border:1px solid var(--line);border-radius:8px;overflow:hidden;font-size:12px;min-width:560px}
.grow-h,.grow{display:grid;grid-template-columns:130px 1fr}
.grow-h{background:#efece4;color:var(--ink2);font-size:11px}
.grow-h .gname{padding:6px 10px}
.gdates{display:grid;position:relative}
.gdates div{padding:5px 0;text-align:center;border-left:1px solid #f0ede6}
.gdates .wk{color:#b3ad9c}
.gbody .grow{border-top:1px solid #f0ede6;position:relative}
.gname{padding:8px 10px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.gname .code{font-weight:400;font-size:11px;display:block}
.gtrack{position:relative;height:34px}
.gtoday{position:absolute;top:0;bottom:0;background:rgba(47,93,80,.07);border-left:1px dashed var(--accent);z-index:0}
.gbar{position:absolute;top:8px;height:18px;background:#bcd8c9;border:1px solid #7fb89a;border-radius:4px;z-index:1}
.gbar.hot{background:#f5c9c0;border-color:#e08a7a}
.gdot{position:absolute;top:13px;width:9px;height:9px;border-radius:50%;transform:translateX(-4px);z-index:2;border:1.5px solid #fff;box-shadow:0 0 0 1px rgba(0,0,0,.08)}
.gstar{position:absolute;top:5px;transform:translateX(-7px);font-size:14px;z-index:2;color:var(--up)}
.glegend{display:flex;gap:14px;flex-wrap:wrap;font-size:11px;color:var(--ink2);margin-top:8px}
.glegend i{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:4px;vertical-align:-1px}
.conflict{background:#fdeeea;border:1px solid #f0c4b8;color:#a04432;border-radius:8px;padding:10px 14px;font-size:12px;margin-top:10px}
/* 明细卡 */
.hkcard{border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-top:12px;background:#fffef9}
.hkcard .hd{display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;align-items:baseline}
.hkcard .nm{font-size:16px;font-weight:700}
.hkcard .facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px 14px;margin-top:10px;font-size:12px}
.hkcard .facts .k{color:var(--ink2);font-size:11px}
.hkcard .facts .v{font-weight:600;margin-top:1px}
.tl{display:flex;align-items:center;margin-top:12px;font-size:11px;flex-wrap:wrap;gap:2px}
.tl .node{text-align:center;min-width:64px}
.tl .node .d{font-weight:600;font-size:12px}
.tl .seg{flex:1;height:2px;background:var(--line);min-width:14px}
.tl .seg.on{background:#7fb89a}
.tl .pt{width:9px;height:9px;border-radius:50%;margin:0 auto 3px}
/* 计算器 */
.calc-ctl{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;margin-bottom:14px;align-items:flex-end}
.calc-ctl label{display:block;color:var(--ink2);font-size:11px;margin-bottom:3px}
.calc-ctl select,.calc-ctl input{border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:13px;background:#fff;color:var(--ink);width:120px}
.calc-ctl .scenBtns{display:flex;gap:6px}
.sbtn{border:1px solid var(--line);background:var(--chip);border-radius:14px;padding:4px 12px;font-size:12px;cursor:pointer;color:var(--ink2)}
.sbtn.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.calc-out{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px}
.calc-card{border:1px solid var(--line);border-radius:10px;padding:14px;background:#fff}
.calc-card h4{font-size:13px;margin-bottom:8px;display:flex;justify-content:space-between}
.calc-card h4 .bp{color:var(--up);font-size:15px}
.calc-card .row{display:flex;justify-content:space-between;font-size:12px;padding:3px 0;color:var(--ink2)}
.calc-card .row b{color:var(--ink);font-weight:600}
.calc-card .risk{font-size:11px;color:#9a6b1a;background:#fdf3e0;border-radius:6px;padding:6px 8px;margin-top:8px}
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <h1>投资策略看板<span class="dot"> · </span>日更数据</h1>
    <div class="meta">
      <span>数据使用期限 <b id="m-date"></b></span>
      <span>看板更新 <b id="m-gen"></b></span>
      <span>来源 <b>腾讯文档 · 日更数据</b></span>
    </div>
    <div id="stale-bar" style="display:none;margin-top:10px;background:#fdeeea;border:1px solid #f0c4b8;color:#a04432;border-radius:8px;padding:8px 14px;font-size:12px"></div>
  </header>

  <nav class="tabs">
    <a href="#overview">总览</a>
    <a href="#ipo">打新日历</a>
    <a href="#lowpremium">转债低溢价轮动</a>
    <a href="#smallcap">小市值轮动</a>
    <a href="#etf">股债ETF轮动</a>
    <a href="#tandabing">转债摊大饼</a>
    <a href="#qdii">QDII基金池</a>
  </nav>

  <section id="overview">
    <h2><span class="bar"></span>策略总览</h2>
    <div class="cards" id="ov-cards"></div>
    <div style="height:14px"></div>
    <div class="insight"><ul id="insight-list" style="padding-left:4px"></ul></div>
  </section>

  <section id="ipo">
    <h2><span class="bar"></span>打新日历 <span class="sub">可转债（来源：东方财富） · 港股（来源：阿斯达克新股中心）</span></h2>
    <div class="panel"><h3>可转债打新 <span class="badge cons" id="cb-n"></span></h3><div style="overflow-x:auto" id="cb-body"></div></div>

    <div class="panel" style="margin-top:18px">
      <h3>港股打新 · 招股日历 <span class="badge aggr" id="hk-n"></span></h3>
      <div id="hk-cal-wrap" style="overflow-x:auto"><div id="hk-cal"></div></div>
      <div id="hk-conflict" style="margin-top:10px"></div>
      <div id="hk-cards" style="margin-top:14px"></div>
    </div>

    <div class="panel" style="margin-top:18px">
      <h3>三种打法 · 打和点计算器 <span class="legend">经验模型估算，非实时认购数据</span></h3>
      <div class="calc-ctl" id="calc-ctl"></div>
      <div class="calc-out" id="calc-out"></div>
      <div class="note" id="calc-note"></div>
    </div>
  </section>

  <section id="lowpremium">
    <h2><span class="bar"></span>转债低溢价轮动 <span class="sub" id="lp-sub"></span></h2>
    <div class="grid2">
      <div class="panel"><h3>保守版 <span class="badge cons" id="lp-cons-n"></span></h3><div id="lp-cons"></div></div>
      <div class="panel"><h3>激进版 <span class="badge aggr" id="lp-aggr-n"></span></h3><div id="lp-aggr"></div></div>
    </div>
    <div class="note" id="lp-note"></div>
  </section>

  <section id="smallcap">
    <h2><span class="bar"></span>小市值轮动 <span class="sub" id="sc-sub"></span></h2>
    <div class="grid2">
      <div class="panel"><h3>保守版 <span class="badge cons" id="sc-cons-n"></span></h3><div id="sc-cons"></div></div>
      <div class="panel"><h3>激进版 <span class="badge aggr" id="sc-aggr-n"></span></h3><div id="sc-aggr"></div></div>
    </div>
  </section>

  <section id="etf">
    <h2><span class="bar"></span>股债ETF轮动 <span class="sub" id="etf-sub"></span></h2>
    <div class="holdcard" id="etf-hold"></div>
    <div class="panel">
      <h3>近 22 个交易日涨幅 <span class="legend"><span class="dotup"></span>上涨 <span class="dotdown"></span>下跌（中国股市配色）</span></h3>
      <div class="barwrap" id="etf-bars"></div>
      <table id="etf-table" style="margin-top:14px"></table>
    </div>
  </section>

  <section id="tandabing">
    <h2><span class="bar"></span>转债到期为正摊大饼 <span class="sub" id="tdb-sub"></span></h2>
    <div class="panel" id="tdb-body"></div>
  </section>

  <section id="qdii">
    <h2><span class="bar"></span>QDII基金池 <span class="sub" id="qd-sub"></span></h2>
    <div class="panel">
      <div class="chips" id="qd-chips"></div>
      <div style="overflow-x:auto"><table id="qd-table"></table></div>
    </div>
  </section>

  <footer>
    <span>数据源自腾讯文档，每早自动更新 · 已按需求排除「债券指数基金池」「中资ETF基金池」</span>
    <a href="__DOC_URL__" target="_blank">打开原始表格 ↗</a>
  </footer>
</div>

<script>
const DATA = __DATA_JSON__;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function fmtDate(d){ return d || '—'; }

function renderOverview(){
  const lp = DATA.low_premium.pools, sc = DATA.small_cap.pools;
  const etfHold = DATA.etf_rotation.holdings.map(h=>`${h.edition||''} ${h.name}`).join(' / ') || '—';
  const tdb = DATA.tandabing;
  const qd = DATA.qdii;
  const cb = DATA.cb_ipo || {apply:[],listing:[]}, hk = DATA.hk_ipo || {stocks:[]};
  const hkStocks = hk.stocks || [];
  const cbToday = cb.apply.filter(x=>x.status==='today').length;
  const cards = [
    {t:'可转债打新', v:String(cb.apply.length), u:'只待申购', d:cbToday?`${cbToday} 只今日可申购！`:(cb.apply.length?`最近申购日 ${cb.apply[0].apply_date}`:'近期无新债申购'), empty:!cb.apply.length},
    {t:'港股打新', v:String(hkStocks.length), u:'只在途', d:hkStocks.length?hkStocks.map(x=>x.name).join('、'):'当前无招股或待上市新股', empty:!hkStocks.length},
    {t:'转债低溢价轮动', v:`${lp['保守版'].length} / ${lp['激进版'].length}`, u:'只 保守/激进', d:'低溢价双池轮动'},
    {t:'小市值轮动', v:`${sc['保守版'].length} / ${sc['激进版'].length}`, u:'只 保守/激进', d:'小市值双池轮动'},
    {t:'股债ETF轮动', v:String(DATA.etf_rotation.holdings.length), u:'个版本持仓', d:etfHold},
    {t:'转债摊大饼', v:`${tdb.holdings.length} / ${tdb.capacity}`, u:'持仓位', d:tdb.holdings.length?'分散持有中':'当前空仓，等待信号', empty:!tdb.holdings.length},
    {t:'QDII基金池', v:String(qd.funds.length), u:'只基金', d:`${qd.categories.length} 个分类`},
  ];
  $('ov-cards').innerHTML = cards.map(c=>`
    <div class="card ${c.empty?'empty':''}">
      <div class="t">${esc(c.t)}</div>
      <div class="v">${esc(c.v)}<small>${esc(c.u)}</small></div>
      <div class="d">${esc(c.d)}</div>
    </div>`).join('');
  $('insight-list').innerHTML = DATA.insights.map(i=>`<li>${esc(i)}</li>`).join('');
}

function poolTable(pool, dupCodes){
  if(!pool.length) return '<div class="empty-state">暂无持仓</div>';
  return `<table><thead><tr><th>#</th><th>代码</th><th>名称</th></tr></thead><tbody>` +
    pool.map(x=>`<tr class="${dupCodes.has(x.code)?'dup':''}">
      <td>${x.num}</td><td class="code">${esc(x.code)}</td><td>${esc(x.name)}${dupCodes.has(x.code)?' <span class="tag limit">双版重合</span>':''}</td>
    </tr>`).join('') + '</tbody></table>';
}

function renderPairs(key, prefix, subId){
  const d = DATA[key], pools = d.pools;
  const dup = new Set(pools['保守版'].map(x=>x.code).filter(c=>new Set(pools['激进版'].map(y=>y.code)).has(c)));
  $(prefix+'-cons').innerHTML = poolTable(pools['保守版'], dup);
  $(prefix+'-aggr').innerHTML = poolTable(pools['激进版'], dup);
  $(prefix+'-cons-n').textContent = pools['保守版'].length + ' 只';
  $(prefix+'-aggr-n').textContent = pools['激进版'].length + ' 只';
  $(subId).textContent = '数据使用期限 ' + fmtDate(d.date);
  if(prefix==='lp' && d.notes && d.notes.length) $('lp-note').textContent = '筛选规则：' + d.notes.join('；');
}

function renderEtf(){
  const d = DATA.etf_rotation;
  $('etf-sub').textContent = '数据使用期限 ' + fmtDate(d.date);
  $('etf-hold').innerHTML = d.holdings.map(h=>`
    <div class="hold ${h.edition==='国际版'?'intl':''}">
      <div class="ed">${esc(h.edition||'持仓')}</div>
      <div class="nm">${esc(h.name)}${h.code?` <span style="font-size:13px;opacity:.8">${esc(h.code)}</span>`:''}</div>
      <div class="dd">${h.days!=null?`已持续 ${h.days} 个交易日`:''}</div>
    </div>`).join('');
  const perf = d.perf.filter(p=>p.chg22!=null);
  const maxAbs = Math.max(...perf.map(p=>Math.abs(p.chg22)), 0.01);
  $('etf-bars').innerHTML = perf.map(p=>{
    const w = Math.abs(p.chg22)/maxAbs*50; // 半轨百分比
    const up = p.chg22 >= 0;
    const style = up ? `left:50%;width:${w}%` : `right:50%;width:${w}%`;
    const color = up ? 'var(--up)' : 'var(--down)';
    return `<div class="barrow">
      <div class="lbl">${esc(p.name)}</div>
      <div class="bartrack"><div class="barzero"></div><div class="barfill" style="${style};background:${color}"></div></div>
      <div class="barval" style="color:${color}">${up?'+':''}${p.chg22.toFixed(2)}%</div>
    </div>`;
  }).join('');
  $('etf-table').innerHTML = `<thead><tr><th>标的</th><th>代码</th><th style="text-align:right">近22日涨幅</th><th style="text-align:right">BIAS</th></tr></thead><tbody>` +
    d.perf.map(p=>{
      const c = p.chg22==null?'':(p.chg22>=0?'var(--up)':'var(--down)');
      const bc = p.bias==null?'':(p.bias>=0?'var(--up)':'var(--down)');
      return `<tr><td>${esc(p.name)}</td><td class="code">${esc(p.code)}</td>
        <td style="text-align:right;color:${c};font-weight:600">${p.chg22==null?'—':(p.chg22>=0?'+':'')+p.chg22.toFixed(2)+'%'}</td>
        <td style="text-align:right;color:${bc}">${p.bias==null?'—':p.bias.toFixed(2)}</td></tr>`;
    }).join('') + '</tbody>';
}

function renderTdb(){
  const d = DATA.tandabing;
  $('tdb-sub').textContent = '数据使用期限 ' + fmtDate(d.date);
  if(!d.holdings.length){
    $('tdb-body').innerHTML = `<div class="empty-state">
      <div class="big">◌</div>
      <div style="font-size:15px;color:var(--ink)">当前空仓</div>
      <div style="font-size:12px;margin-top:4px">策略容量 ${d.capacity} 只，暂无符合条件的「到期收益为正」转债，等待每日数据更新</div>
    </div>`;
    return;
  }
  $('tdb-body').innerHTML = `<table><thead><tr><th>#</th><th>转债代码</th><th>转债名称</th></tr></thead><tbody>` +
    d.holdings.map(x=>`<tr><td>${x.num}</td><td class="code">${esc(x.code)}</td><td>${esc(x.name)}</td></tr>`).join('') + '</tbody></table>';
}

function renderQdii(){
  const d = DATA.qdii;
  $('qd-sub').textContent = d.title ? d.title.replace(/^.*?（/,'（') : ('数据使用期限 ' + fmtDate(d.date));
  const chips = ['全部', ...d.categories];
  $('qd-chips').innerHTML = chips.map((c,i)=>`<span class="chip ${i===0?'on':''}" data-cat="${esc(c)}">${esc(c)}</span>`).join('');
  const statusTag = s => {
    if(!s) return '';
    if(s.includes('暂停')) return `<span class="tag pause">${esc(s)}</span>`;
    if(s.includes('不限购')) return `<span class="tag free">${esc(s)}</span>`;
    return `<span class="tag limit">${esc(s)}</span>`;
  };
  const renderRows = cat => {
    const rows = d.funds.filter(f=>cat==='全部'||f.category===cat);
    $('qd-table').innerHTML = `<thead><tr><th>分类</th><th>代码</th><th>基金名称</th><th>限购状态</th><th style="text-align:right">限购金额(元/日)</th><th style="text-align:right">管理费</th><th style="text-align:right">托管费</th><th style="text-align:right">销售服务费</th><th>备注</th></tr></thead><tbody>` +
      rows.map(f=>`<tr>
        <td>${esc(f.category)}</td><td class="code">${esc(f.code)}</td><td>${esc(f.name)}</td>
        <td>${statusTag(f.status)}</td>
        <td style="text-align:right">${f.limit?esc(f.limit):'—'}</td>
        <td style="text-align:right">${esc(f.mgmt_fee)}</td>
        <td style="text-align:right">${esc(f.custody_fee)}</td>
        <td style="text-align:right">${esc(f.sales_fee)}</td>
        <td>${esc(f.duration)}</td>
      </tr>`).join('') + '</tbody>';
  };
  renderRows('全部');
  $('qd-chips').addEventListener('click', e=>{
    const el = e.target.closest('.chip'); if(!el) return;
    document.querySelectorAll('#qd-chips .chip').forEach(c=>c.classList.remove('on'));
    el.classList.add('on');
    renderRows(el.dataset.cat);
  });
}

function renderCbIpo(){
  const cb = DATA.cb_ipo || {apply:[],listing:[],error:''};
  $('cb-n').textContent = `${cb.apply.length} 申购 / ${cb.listing.length} 待上市`;
  if(cb.error && !cb.apply.length && !cb.listing.length){
    $('cb-body').innerHTML = `<div class="empty-state">数据源暂不可用<span style="font-size:11px;display:block;margin-top:4px">${esc(cb.error)}</span></div>`;
  } else if(!cb.apply.length && !cb.listing.length){
    $('cb-body').innerHTML = '<div class="empty-state">近期无可转债申购或上市</div>';
  } else {
    const statusTag = it => {
      if(it.status==='today') return '<span class="tag hot">今日申购</span>';
      if(it.status==='apply') return `<span class="tag soon">${esc(it.apply_date)} 申购</span>`;
      return `<span class="tag free">待上市 ${esc(it.listing_date||'待定')}</span>`;
    };
    const rows = [...cb.apply, ...cb.listing];
    $('cb-body').innerHTML = `<table><thead><tr><th>转债名称</th><th>申购代码</th><th>正股</th><th style="text-align:right">发行规模(亿)</th><th>评级</th><th style="text-align:right">转股价</th><th>状态</th></tr></thead><tbody>` +
      rows.map(it=>`<tr class="${it.status==='today'?'dup':''}">
        <td>${esc(it.name)}<div class="code" style="font-size:11px">${esc(it.code)}</div></td>
        <td class="code">${esc(it.apply_code)}</td>
        <td>${esc(it.stock_name)}</td>
        <td style="text-align:right">${it.scale!=null?Number(it.scale).toFixed(2):'—'}</td>
        <td>${esc(it.rating)||'—'}</td>
        <td style="text-align:right">${it.transfer_price??'—'}</td>
        <td>${statusTag(it)}</td>
      </tr>`).join('') + '</tbody></table>';
  }
}

/* ---------- 港股打新工作台 ---------- */
const HKFEE = 0.0100835; // 中签费用率: 经纪佣金1% + 证监会征费0.0027% + 交易所费0.00565%（阿斯达克招股计算机口径）
const SCEN = [
  {k:'cold', t:'冷淡', sub:'<15倍认购', p1:0.60, p1r:'30%~100%', stable:'1~2 手', claw:'不回拨（10%）'},
  {k:'warm', t:'一般', sub:'15~50倍', p1:0.18, p1r:'10%~30%', stable:'5~20 手', claw:'回拨至30%'},
  {k:'hot',  t:'热门', sub:'50~100倍', p1:0.06, p1r:'3%~10%', stable:'30~80 手', claw:'回拨至40%'},
  {k:'fire', t:'爆热', sub:'>100倍', p1:0.025, p1r:'1%~5%', stable:'100~300 手', claw:'回拨至50%'},
];
let calcState = {stock:0, scen:'warm', rate:5.5, mlots:10, acc:5};

function hkParseNum(s){ const n = parseFloat(String(s||'').replace(/,/g,'')); return isNaN(n)?null:n; }
function hkPriceHigh(s){ const m = String(s||'').match(/([\d.]+)\s*$/); return m?parseFloat(m[1]):null; }
function daysBetween(a,b){ if(!a||!b) return null; return Math.round((new Date(b)-new Date(a))/86400000); }

function renderHkIpo(){
  const hk = DATA.hk_ipo || {stocks:[],conflicts:[],error:'',source:''};
  const stocks = hk.stocks || [];
  $('hk-n').textContent = stocks.length + ' 只';
  const cal = $('hk-cal'), cards = $('hk-cards'), conf = $('hk-conflict');
  if(!stocks.length){
    cal.innerHTML = `<div class="empty-state">${hk.error?('数据源暂不可用<span style="font-size:11px;display:block;margin-top:4px">'+esc(hk.error)+'</span>'):'当前无招股中或待上市港股新股'}</div>`;
    cards.innerHTML = ''; conf.innerHTML = '';
    renderCalc();
    return;
  }
  // ---- 甘特日历 ----
  const today = new Date(); today.setHours(0,0,0,0);
  const ds = [];
  stocks.forEach(s=>['apply_start','apply_end','pricing_date','result_date','dark_date','listing_date'].forEach(k=>s[k]&&ds.push(s[k])));
  let d0 = new Date(Math.min(today, ...ds.map(d=>new Date(d))));
  let d1 = new Date(Math.max(today, ...ds.map(d=>new Date(d))));
  d0.setDate(d0.getDate()-1); d1.setDate(d1.getDate()+1);
  const days = [];
  for(let d=new Date(d0); d<=d1; d.setDate(d.getDate()+1)) days.push(new Date(d));
  const N = days.length, DW = 26;
  const tstr = d => d.toISOString().slice(0,10);
  const idx = s => { const t = new Date(s); t.setHours(0,0,0,0); return Math.round((t-days[0])/86400000); };
  const WD = '日一二三四五六';
  const conflictNames = new Set((hk.conflicts||[]).flatMap(c=>c.pair));
  const todayI = idx(tstr(today));
  let html = `<div class="gantt" style="width:${130+N*DW}px">`;
  html += `<div class="grow-h"><div class="gname">新股 / 日期</div><div class="gdates" style="grid-template-columns:repeat(${N},${DW}px)">` +
    days.map(d=>`<div class="${(d.getDay()===0||d.getDay()===6)?'wk':''}">${d.getMonth()+1}/${d.getDate()}<br>${WD[d.getDay()]}</div>`).join('') + '</div></div><div class="gbody">';
  stocks.forEach(s=>{
    html += `<div class="grow"><div class="gname">${esc(s.name)}<span class="code">${esc(s.code)}</span></div><div class="gtrack" style="width:${N*DW}px">`;
    if(todayI>=0 && todayI<N) html += `<div class="gtoday" style="left:${todayI*DW}px;width:${DW}px"></div>`;
    if(s.apply_start && s.apply_end){
      const x0=idx(s.apply_start), x1=idx(s.apply_end);
      html += `<div class="gbar ${conflictNames.has(s.name)?'hot':''}" style="left:${x0*DW+2}px;width:${(x1-x0+1)*DW-4}px"></div>`;
    }
    const dot = (d,c,tit)=> d?`<div class="gdot" title="${tit} ${d}" style="left:${idx(d)*DW+DW/2}px;background:${c}"></div>`:'';
    html += dot(s.apply_end,'#e08a3a','招股截止') + dot(s.pricing_date,'#9a958a','定价') + dot(s.result_date,'#8a6dbb','公布中签') + dot(s.dark_date,'#4a7fbf','暗盘');
    if(s.listing_date) html += `<div class="gstar" title="上市 ${s.listing_date}" style="left:${idx(s.listing_date)*DW+DW/2}px">◆</div>`;
    html += '</div></div>';
  });
  html += '</div></div>';
  html += `<div class="glegend"><span><i style="background:#7fb89a"></i>招股期（资金占用至公布中签）</span><span><i style="background:#e08a3a"></i>截止</span><span><i style="background:#9a958a"></i>定价</span><span><i style="background:#8a6dbb"></i>公布中签</span><span><i style="background:#4a7fbf"></i>暗盘</span><span style="color:var(--up)">◆ 上市首日</span><span>虚线列 = 今天</span></div>`;
  cal.innerHTML = html;
  // ---- 资金冲突 ----
  conf.innerHTML = (hk.conflicts||[]).map(c=>`<div class="conflict">⚠️ <b>资金冲突</b>：${esc(c.pair[0])} 与 ${esc(c.pair[1])} 的申购资金冻结期在 ${c.overlap_start} ~ ${c.overlap_end} 重叠，同一笔资金无法两头兼顾，请提前分配。</div>`).join('');
  // ---- 明细卡 ----
  cards.innerHTML = stocks.map(s=>{
    const steps = [
      ['招股开始', s.apply_start, '#7fb89a'], ['招股截止', s.apply_end, '#e08a3a'], ['定价', s.pricing_date, '#9a958a'],
      ['公布中签', s.result_date, '#8a6dbb'], ['暗盘', s.dark_date, '#4a7fbf'], ['上市首日', s.listing_date, 'var(--up)'],
    ];
    const tl = steps.map((st,i)=>{
      const on = st[1] && tstr(today) >= st[1];
      return `${i?`<div class="seg ${steps[i-1][1]&&st[1]&&tstr(today)>=steps[i-1][1]?'on':''}"></div>`:''}<div class="node"><div class="pt" style="background:${st[1]?st[2]:'#d8d3c6'}"></div><div class="d" style="${st[1]?'':'color:#b3ad9c'}">${st[1]?st[1].slice(5):'待定'}</div><div style="color:var(--ink2)">${st[0]}</div></div>`;
    }).join('');
    const f = (k,v)=>`<div><div class="k">${k}</div><div class="v">${v||'—'}</div></div>`;
    return `<div class="hkcard">
      <div class="hd"><div class="nm">${esc(s.name)} <span class="code" style="font-size:12px">${esc(s.code)}.HK</span></div>
      <div>${s.stage?`<span class="tag hot">${esc(s.stage)}</span>`:''} <span class="tag soon">${esc(s.industry)||'—'}</span></div></div>
      <div class="facts">
        ${f('招股价(港元)', esc(s.price_range))}
        ${f('每手股数', esc(s.lot_size))}
        ${f('入场费(港元)', esc(s.entry_fee))}
        ${f('保荐人', esc(s.sponsor))}
        ${f('公开发售', s.public_offer_lots?Number(s.public_offer_lots).toLocaleString()+' 手('+(s.public_offer_pct||'?')+'%)':'—')}
        ${f('市值区间(港元)', s.mkt_cap?esc(s.mkt_cap):'—')}
      </div>
      <div class="tl">${tl}</div>
    </div>`;
  }).join('');
  renderCalc();
}

/* ---------- 打和点计算器 ---------- */
function renderCalc(){
  const stocks = (DATA.hk_ipo||{}).stocks || [];
  const ctl = $('calc-ctl'), out = $('calc-out'), note = $('calc-note');
  if(!stocks.length){ ctl.innerHTML=''; out.innerHTML='<div class="empty-state">暂无在途新股，计算器待命中</div>'; note.innerHTML=''; return; }
  const s = stocks[Math.min(calcState.stock, stocks.length-1)];
  calcState.stock = Math.min(calcState.stock, stocks.length-1);
  ctl.innerHTML = `
    <div><label>选择新股</label><select id="c-stock">${stocks.map((x,i)=>`<option value="${i}" ${i===calcState.stock?'selected':''}>${esc(x.name)} ${esc(x.code)}</option>`).join('')}</select></div>
    <div><label>热度情景（认购倍数）</label><div class="scenBtns">${SCEN.map(x=>`<span class="sbtn ${x.k===calcState.scen?'on':''}" data-k="${x.k}">${x.t}</span>`).join('')}</div></div>
    <div><label>融资年利率 %</label><input id="c-rate" type="number" step="0.1" value="${calcState.rate}"></div>
    <div><label>融资申购手数</label><input id="c-mlots" type="number" min="1" value="${calcState.mlots}"></div>
    <div><label>一手摸户数</label><input id="c-acc" type="number" min="1" max="20" value="${calcState.acc}"></div>`;
  ctl.querySelector('#c-stock').onchange = e=>{calcState.stock=+e.target.value; renderCalc();};
  ctl.querySelectorAll('.sbtn').forEach(b=>b.onclick=()=>{calcState.scen=b.dataset.k; renderCalc();});
  ctl.querySelector('#c-rate').onchange = e=>{calcState.rate=+e.target.value||5.5; renderCalc();};
  ctl.querySelector('#c-mlots').onchange = e=>{calcState.mlots=Math.max(1,+e.target.value||10); renderCalc();};
  ctl.querySelector('#c-acc').onchange = e=>{calcState.acc=Math.max(1,+e.target.value||5); renderCalc();};

  const scen = SCEN.find(x=>x.k===calcState.scen);
  const price = hkPriceHigh(s.price_range) || 0;
  const lot = hkParseNum(s.lot_size) || 0;
  const lotVal = price * lot;                          // 一手货值（按招股价上限，港元）
  const entry = hkParseNum(s.entry_fee) || lotVal*(1+HKFEE);
  const freezeDays = daysBetween(s.apply_end, s.result_date) ?? 3;
  const fmt = n => n.toLocaleString('zh-CN',{maximumFractionDigits:2});
  const pct = n => (n*100).toFixed(2)+'%';

  // ① 现金一手
  const cashCost = lotVal*HKFEE;                        // 中签后费用
  const cashBp = lotVal? cashCost/lotVal : 0;
  // ② 融资 N 手（借 90%）
  const sub = lotVal*calcState.mlots;
  const loan = sub*0.9;
  const interest = loan*(calcState.rate/100)*Math.max(freezeDays,1)/365;
  const marginFee = 100;                                 // 融资手续费假设
  const k1Cost = interest+marginFee+lotVal*HKFEE;
  const bp1 = lotVal? k1Cost/lotVal : 0;                 // 仅中 1 手
  const kExp = Math.max(1, Math.round(scen.p1*calcState.mlots));
  const bpK = lotVal? (interest+marginFee+kExp*lotVal*HKFEE)/(kExp*lotVal) : 0;
  // ③ 一手摸 M 户
  const expLots = calcState.acc*scen.p1;
  const accInvest = calcState.acc*entry;
  const bpAcc = (expLots>0&&lotVal)? (expLots*lotVal*HKFEE)/(expLots*lotVal) : 0;

  out.innerHTML = `
    <div class="calc-card"><h4>① 现金一手 <span class="bp">打和点 +${pct(cashBp)}</span></h4>
      <div class="row"><span>投入（入场费）</span><b>${fmt(entry)} 港元</b></div>
      <div class="row"><span>一手中签率（${scen.t}档）</span><b>${scen.p1r}</b></div>
      <div class="row"><span>稳中一手约需</span><b>${scen.stable}</b></div>
      <div class="row"><span>中签后费用（${(HKFEE*100).toFixed(3)}%）</span><b>${fmt(cashCost)} 港元</b></div>
      <div class="risk">未中签损失≈0（现金申购多数券商免费）；破发风险自担。</div>
    </div>
    <div class="calc-card"><h4>② 融资 ${calcState.mlots} 手（孖展90%） <span class="bp">+${pct(bp1)} ~ +${pct(bpK)}</span></h4>
      <div class="row"><span>申购货值 / 借款</span><b>${fmt(sub)} / ${fmt(loan)} 港元</b></div>
      <div class="row"><span>利息（${calcState.rate}% × ${Math.max(freezeDays,1)}天）</span><b>${fmt(interest)} 港元</b></div>
      <div class="row"><span>手续费（假设）</span><b>${fmt(marginFee)} 港元</b></div>
      <div class="row"><span>若仅中 1 手，打和点</span><b style="color:var(--up)">+${pct(bp1)}</b></div>
      <div class="row"><span>若中 ${kExp} 手（期望），打和点</span><b>+${pct(bpK)}</b></div>
      <div class="risk">融资打新的命门：中签越少打和点越高。仅中 1 手时需涨 ${pct(bp1)} 才回本——冷门股慎用杠杆。</div>
    </div>
    <div class="calc-card"><h4>③ 一手摸 × ${calcState.acc} 户 <span class="bp">期望中签 ${expLots.toFixed(2)} 手</span></h4>
      <div class="row"><span>总投入</span><b>${fmt(accInvest)} 港元</b></div>
      <div class="row"><span>每户一手中签率</span><b>${scen.p1r}</b></div>
      <div class="row"><span>期望中签货值</span><b>${fmt(expLots*lotVal)} 港元</b></div>
      <div class="row"><span>期望打和点</span><b>+${pct(bpAcc)}</b></div>
      <div class="risk">多户申购受券商政策与实名制约束，请确认合规后操作。</div>
    </div>`;
  note.innerHTML = `模型说明：中签费用率 ${(HKFEE*100).toFixed(4)}%（佣金1%+征费0.0027%+交易费0.00565%，阿斯达克招股计算机口径）；资金冻结 ${Math.max(freezeDays,1)} 天（截止 ${s.apply_end||'?'} → 公布中签 ${s.result_date||'?'}）；利率/手续费为假设值可调。中签率档位为历史经验区间——锚点：MOMENTA-W(06880) 2026年8月超购 412.63 倍 → 一手中签率 5%、300 手稳一手（阿斯达克公开报道）。<b>以上均为模型估算，非实时认购数据，不构成投资建议。</b>公开发售 ${s.public_offer_lots?Number(s.public_offer_lots).toLocaleString()+' 手':''}，${scen.claw}。`;
}

(function init(){
  const dates = [DATA.low_premium.date, DATA.small_cap.date, DATA.etf_rotation.date, DATA.tandabing.date].filter(Boolean);
  $('m-date').textContent = dates[0] || '—';
  $('m-gen').textContent = DATA.generated_at;
  if(DATA.stale_notice){ const b=$('stale-bar'); b.textContent='⚠️ '+DATA.stale_notice; b.style.display='block'; }
  renderOverview();
  renderCbIpo();
  renderHkIpo();
  renderPairs('low_premium','lp','lp-sub');
  renderPairs('small_cap','sc','sc-sub');
  renderEtf();
  renderTdb();
  renderQdii();
  if('serviceWorker' in navigator && location.protocol === 'https:'){
    navigator.serviceWorker.register('sw.js').catch(()=>{});
  }
})();
</script>
</body>
</html>
"""


# ---------- 主流程 ----------
def main():
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 开始拉取腾讯文档数据…")
    try:
        grids = {}
        for sid, name, rows, cols in SHEETS:
            grids[sid] = fetch_sheet(sid, rows, cols)
            print(f"  ✓ {name} ({sid}) {len(grids[sid])} 行")
        data = {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "doc_url": DOC_URL,
            "tandabing": parse_tandabing(grids["BB08J2"]),
            "low_premium": parse_rotation(grids["BB08J3"], "转债代码"),
            "small_cap": parse_rotation(grids["BB08J4"], "股票代码"),
            "etf_rotation": parse_etf_rotation(grids["BB08J5"]),
            "qdii": parse_qdii(grids["7o3245"]),
        }
    except Exception as e:
        # 腾讯文档失败（云端票据过期等）：用上次发布的数据降级，看板挂警示
        if not FALLBACK_JSON.exists():
            raise
        print(f"  ! 腾讯文档拉取失败（{str(e)[:150]}），改用上次缓存数据降级", file=sys.stderr)
        old = json.loads(FALLBACK_JSON.read_text(encoding="utf-8"))
        data = {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "doc_url": DOC_URL,
            "tandabing": old["tandabing"], "low_premium": old["low_premium"],
            "small_cap": old["small_cap"], "etf_rotation": old["etf_rotation"],
            "qdii": old["qdii"],
        }
        data["stale_notice"] = (f"策略数据为 {old.get('generated_at','上次')} 缓存：腾讯文档授权可能已过期，"
                                f"请打开 WorkBuddy 触发一次本地更新以刷新票据")
    print("  拉取打新数据…")
    data["cb_ipo"] = fetch_cb_ipo()
    data["hk_ipo"] = fetch_hk_ipo()
    if data["cb_ipo"]["error"]:
        print(f"  ! 可转债打新数据源异常: {data['cb_ipo']['error']}")
    if data["hk_ipo"]["error"]:
        print(f"  ! 港股打新数据源异常: {data['hk_ipo']['error']}")
    data["insights"] = build_insights(data)
    if data.get("stale_notice"):
        data["insights"].insert(0, "⚠️ " + data["stale_notice"])

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    html = render_html(data).replace("__DOC_URL__", DOC_URL)
    OUT_HTML.write_text(html, encoding="utf-8")
    DEPLOY_HTML.parent.mkdir(parents=True, exist_ok=True)
    DEPLOY_HTML.write_text(html, encoding="utf-8")
    # 数据副本随站发布：供云端下次运行时做降级基底
    (DEPLOY_HTML.parent / "dashboard_data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 看板已生成 → {OUT_HTML}（已同步 deploy/）")
    print(f"  数据使用期限: {data['low_premium']['date'] or '—'}")
    print(f"  低溢价 保守/激进: {len(data['low_premium']['pools']['保守版'])}/{len(data['low_premium']['pools']['激进版'])} 只")
    print(f"  小市值 保守/激进: {len(data['small_cap']['pools']['保守版'])}/{len(data['small_cap']['pools']['激进版'])} 只")
    print(f"  摊大饼持仓: {len(data['tandabing']['holdings'])} 只")
    print(f"  QDII 基金: {len(data['qdii']['funds'])} 只")
    print(f"  转债打新: 待申购 {len(data['cb_ipo']['apply'])} / 待上市 {len(data['cb_ipo']['listing'])}")
    print(f"  港股打新: {len(data['hk_ipo'].get('stocks', []))} 只")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)
