import json
import os
import re
from datetime import datetime
from playwright.sync_api import sync_playwright

TISA_URL = "https://www.fundrich.com.tw/themeFund/promotionDetail?type=root.TISA.FundList_Web"

ETF_BENCHMARKS = {
    "0050": {
        "2330 台積電": 54.2,
        "2317 鴻海": 5.1,
        "2454 聯發科": 4.3,
        "2308 台達電": 2.1,
    },
    "00878": {
        "2382 廣達": 4.8,
        "3231 緯創": 4.2,
        "2881 富邦金": 4.5,
        "2882 國泰金": 4.1,
    },
}

ISSUER_KEYWORDS = [
    "元大", "國泰", "富邦", "群益", "統一", "野村", "安聯", "摩根", "貝萊德",
    "聯博", "施羅德", "復華", "保德信", "玉山", "台新", "永豐", "兆豐", "第一金",
    "華南永昌", "凱基", "中信", "街口", "瀚亞", "景順", "富達", "鋒裕匯理",
    "路博邁", "百達", "瑞銀", "柏瑞", "新光", "合庫", "台壽", "匯豐"
]


def load_json_if_exists(path: str, default: dict) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def infer_issuer(name: str) -> str:
    for kw in ISSUER_KEYWORDS:
        if kw in name:
            return f"{kw}投信"
    return "TISA合作投信"


def infer_tisa_type(name: str) -> str:
    upper_name = name.upper()
    if "TISA" in upper_name:
        return "TISA級別"
    if "好好退休" in name:
        return "好好退休"
    if "好享退" in name:
        return "好享退"
    return "TISA專區"


def parse_visible_pc_rows(page) -> list[dict]:
    rows_data = page.evaluate(
        """() => {
            const rows = Array.from(document.querySelectorAll('fr-fund-table-row'));
            return rows.map(row => {
                if (row.offsetParent === null) return null;
                const cols = Array.from(row.querySelectorAll(':scope > div > .col, .col')).map(c =>
                    (c.innerText || '').trim().replace(/\\s+/g, ' ')
                );
                const fullText = (row.innerText || '').trim();
                return { cols, fullText };
            }).filter(Boolean);
        }"""
    )

    parsed = []
    for r in rows_data:
        cols = r.get("cols", [])
        full_text = r.get("fullText", "")
        if not full_text:
            continue

        fid = ""
        fname = ""
        for idx, c in enumerate(cols):
            c_clean = c.strip()
            if re.fullmatch(r"[A-Za-z]{2,4}[0-9]{2,4}[A-Za-z0-9]?", c_clean):
                if c_clean.upper() not in {"TISA", "RR1", "RR2", "RR3", "RR4", "RR5"}:
                    fid = c_clean.upper()
                    if idx + 1 < len(cols) and len(cols[idx + 1]) >= 4:
                        fname = cols[idx + 1].strip()
                    break

        if not fid:
            m_code = re.search(r"\b([A-Za-z]{2,4}[0-9]{2,4}[A-Za-z0-9]?)\b", full_text)
            if m_code and m_code.group(1).upper() not in {"TISA", "RR1", "RR2", "RR3", "RR4", "RR5"}:
                fid = m_code.group(1).upper()

        if not fid:
            continue

        if not fname or "基金" not in fname:
            lines = [ln.strip() for ln in full_text.splitlines() if ln.strip()]
            fname = next(
                (ln for ln in lines if "基金" in ln and len(ln) >= 4),
                fname or f"代碼：{fid}",
            )

        rr_match = re.search(r"\b(RR[1-5])\b", full_text, re.I)
        parsed.append(
            {
                "id": fid,
                "name": fname,
                "issuer": infer_issuer(fname),
                "tisa_type": infer_tisa_type(fname),
                "risk_level": rr_match.group(1).upper() if rr_match else "RR4",
            }
        )

    return parsed


def scrape_fundrich_tisa_list() -> dict:
    json_endpoints = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="zh-TW",
        )
        page = context.new_page()

        def on_response(response):
            ct = (response.headers.get("content-type") or "").lower()
            if "json" in ct or "api" in response.url.lower() or "fund" in response.url.lower():
                json_endpoints.append({
                    "url": response.url,
                    "method": response.request.method,
                    "status": response.status,
                })

        page.on("response", on_response)
        page.goto(TISA_URL, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_selector("fr-fund-table-row", timeout=25000)
        except Exception:
            pass
        page.wait_for_timeout(3500)

        items = parse_visible_pc_rows(page)
        print(f"[DEBUG] 列表解析到 {len(items)} 檔")
        print(f"[DEBUG] JSON APIs: {json.dumps(json_endpoints[:25], ensure_ascii=False)}")

        body_text = page.inner_text("body")
        holdings_date_match = re.search(r"持股資料日[：:\s]*(\d{4}/\d{2}/\d{2})", body_text)
        holdings_as_of = holdings_date_match.group(1) if holdings_date_match else "2026/10/31"
        browser.close()

    return {
        "holdings_as_of": holdings_as_of,
        "funds": items,
    }


def run_fetch():
    prev_raw = load_json_if_exists("data/raw_funds.json", {"funds": []})
    prev_map = {item["id"]: item for item in prev_raw.get("funds", [])}

    try:
        scraped = scrape_fundrich_tisa_list()
        scraped_funds = scraped.get("funds", [])
        holdings_as_of = scraped.get("holdings_as_of", "2026/10/31")
    except Exception as exc:
        print(f"[ERROR] 基富通 TISA 專區抓取失敗：{exc}")
        scraped_funds = []
        holdings_as_of = prev_raw.get("holdings_as_of", "2026/10/31")

    if not scraped_funds:
        print("[WARN] 本次未解析到基金列表。")

    merged_funds = []
    for item in scraped_funds:
        fid = item["id"]
        old = prev_map.get(fid, {})
        merged_funds.append(
            {
                "id": fid,
                "name": item["name"],
                "issuer": item["issuer"],
                "tisa_type": item.get("tisa_type", "TISA專區"),
                "category": old.get("category", "TISA精選基金"),
                "risk_level": item["risk_level"],
                "ter_pct": old.get("ter_pct", 0.0),
                "rule_note": "基富通 TISA 專區適用標的（享專屬經理費與0手續費優惠）",
                "tech_exposure_pct": old.get("tech_exposure_pct", 0.0),
                "periods_per_year": old.get("periods_per_year", 252),
                "prev_holdings": old.get("curr_holdings", {}),
                "curr_holdings": old.get("curr_holdings", {}),
                "nav_history": old.get("nav_history", []),
            }
        )

    os.makedirs("data", exist_ok=True)
    payload = {
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": "基富通 FundRich TISA 專區",
        "source_url": TISA_URL,
        "holdings_as_of": holdings_as_of,
        "total_count": len(merged_funds),
        "etf_benchmarks": ETF_BENCHMARKS,
        "funds": merged_funds,
    }
    with open("data/raw_funds.json", "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
    print(f"已成功產出 data/raw_funds.json，共 {len(merged_funds)} 檔基金")


if __name__ == "__main__":
    run_fetch()
