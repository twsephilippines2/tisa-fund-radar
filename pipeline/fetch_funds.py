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
    "路博邁", "百達", "瑞銀", "柏瑞", "新光", "合庫", "台壽"
]


def load_json_if_exists(path: str, default: dict) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def infer_issuer(name: str, context_text: str) -> str:
    combined = f"{name} {context_text}"
    for kw in ISSUER_KEYWORDS:
        if kw in combined:
            return f"{kw}投信"
    return "TISA合作投信"


def parse_rows_on_current_view(page) -> list[dict]:
    """從目前畫面上的 fr-fund-table-row 擷取基金代碼、名稱與風險等級"""
    raw_rows = page.evaluate(
        """() => {
            const rows = Array.from(document.querySelectorAll('fr-fund-table-row'));
            return rows.map((row, idx) => {
                // 排除收藏/比較 action 欄位，聚焦名稱與其餘資料欄位
                const titleCol = row.querySelector('.col--title, [class*="title"], [class*="name"]');
                const titleText = titleCol ? (titleCol.innerText || '').trim() : '';
                const fullText = (row.innerText || '').trim();
                const link = row.querySelector('a[href]');
                const href = link ? (link.getAttribute('href') || '') : '';
                return {
                    idx,
                    titleText,
                    fullText,
                    href
                };
            });
        }"""
    )

    parsed = []
    for r in raw_rows:
        full_text = r.get("fullText", "")
        title_text = r.get("titleText", "")
        href = r.get("href", "")
        if not full_text:
            continue

        lines = [ln.strip() for ln in full_text.splitlines() if ln.strip()]
        title_lines = [ln.strip() for ln in title_text.splitlines() if ln.strip()]

        # 1. 擷取基金代碼（優先從 href 或文字中的標準代碼格式）
        href_match = re.search(r"(?:fundCode=|code=|/fund/)([A-Z0-9]{4,10})", href, re.I)
        code_matches = re.findall(r"\b([A-Z]{2,4}[0-9]{2,4}[A-Z0-9]?)\b", full_text)
        fid = ""
        if href_match:
            fid = href_match.group(1).upper()
        elif code_matches:
            for c in code_matches:
                cu = c.upper()
                if cu not in {"TISA", "RR1", "RR2", "RR3", "RR4", "RR5", "HTML", "HTTP", "HTTPS"}:
                    fid = cu
                    break
        if not fid:
            continue

        # 2. 挑選完整基金名稱（排除「收藏」、「比較」與純代碼行）
        ignore_exact = {"收藏", "比較", fid, f"代碼：{fid}", f"代碼:{fid}"}
        candidate_lines = title_lines + lines
        best_name = ""
        for ln in candidate_lines:
            cleaned = re.sub(r"^(代碼[：:]\s*[A-Z0-9]+|基金代號[：:]\s*[A-Z0-9]+)\s*", "", ln).strip()
            if not cleaned or cleaned in ignore_exact:
                continue
            if "基金" in cleaned and len(cleaned) >= 4:
                best_name = cleaned
                break
        if not best_name:
            for ln in candidate_lines:
                cleaned = re.sub(r"^(代碼[：:]\s*[A-Z0-9]+|基金代號[：:]\s*[A-Z0-9]+)\s*", "", ln).strip()
                if cleaned and cleaned not in ignore_exact and len(cleaned) >= 4 and not re.fullmatch(r"[A-Z0-9]{4,8}", cleaned):
                    best_name = cleaned
                    break
        if not best_name:
            best_name = f"代碼：{fid}"

        rr_match = re.search(r"\b(RR[1-5])\b", full_text)
        parsed.append(
            {
                "id": fid,
                "name": best_name,
                "issuer": infer_issuer(best_name, full_text),
                "risk_level": rr_match.group(1) if rr_match else "RR4",
            }
        )

    return parsed


def click_next_page_if_available(page) -> bool:
    """嘗試點擊分頁元件中的下一頁按鈕，成功翻頁回傳 True"""
    before_signature = page.evaluate(
        """() => {
            const rows = Array.from(document.querySelectorAll('fr-fund-table-row'));
            return rows.map(r => (r.innerText || '').trim().slice(0, 40)).join('|');
        }"""
    )

    # 常見下一頁選擇器與文字
    next_locators = [
        page.locator("button[aria-label*='下一頁'], a[aria-label*='下一頁'], [class*='next']:not([class*='disabled']) button, [class*='next']:not([class*='disabled']) a, button[class*='next'], a[class*='next']"),
        page.get_by_role("button", name=re.compile(r"^(下一頁|>|›|»)$")),
        page.get_by_role("link", name=re.compile(r"^(下一頁|>|›|»)$")),
    ]

    for loc in next_locators:
        try:
            count = loc.count()
            for i in range(count):
                el = loc.nth(i)
                if not el.is_visible(timeout=500):
                    continue
                is_disabled = el.is_disabled()
                aria_disabled = (el.get_attribute("aria-disabled") or "").lower() == "true"
                cls_attr = (el.get_attribute("class") or "").lower()
                if is_disabled or aria_disabled or "disabled" in cls_attr:
                    continue

                el.scroll_into_view_if_needed(timeout=1000)
                el.click(timeout=2000)
                page.wait_for_timeout(1800)

                after_signature = page.evaluate(
                    """() => {
                        const rows = Array.from(document.querySelectorAll('fr-fund-table-row'));
                        return rows.map(r => (r.innerText || '').trim().slice(0, 40)).join('|');
                    }"""
                )
                if after_signature and after_signature != before_signature:
                    return True
        except Exception:
            continue

    return False


def scrape_fundrich_tisa_list() -> dict:
    """使用無頭 Chromium 載入基富通 TISA 專區並跨分頁擷取完整基金清單"""
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
        page.goto(TISA_URL, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_selector("fr-fund-table-row", timeout=25000)
        except Exception:
            pass
        page.wait_for_timeout(3000)

        # 若有「顯示全部」或「每頁筆數」下拉/按鈕可先嘗試展開
        try:
            all_btn = page.get_by_role("button", name=re.compile(r"全部|All")).first
            if all_btn.is_visible(timeout=500):
                all_btn.click(timeout=1500)
                page.wait_for_timeout(1500)
        except Exception:
            pass

        collected_by_id = {}
        max_pages = 15

        for page_num in range(1, max_pages + 1):
            page_items = parse_rows_on_current_view(page)
            added_this_page = 0
            for item in page_items:
                fid = item["id"]
                if fid not in collected_by_id:
                    collected_by_id[fid] = item
                    added_this_page += 1
            print(f"[DEBUG] 第 {page_num} 頁解析到 {len(page_items)} 列，新增 {added_this_page} 檔（累計 {len(collected_by_id)} 檔）")

            moved = click_next_page_if_available(page)
            if not moved or added_this_page == 0:
                break

        body_text = page.inner_text("body")
        holdings_date_match = re.search(r"持股資料日[：:\s]*(\d{4}/\d{2}/\d{2})", body_text)
        holdings_as_of = holdings_date_match.group(1) if holdings_date_match else "2026/10/31"

        browser.close()

    return {
        "holdings_as_of": holdings_as_of,
        "funds": list(collected_by_id.values()),
    }


def run_fetch():
    prev_raw = load_json_if_exists("data/raw_funds.json", {"funds": []})
    prev_map = {item["id"]: item for item in prev_raw.get("funds", [])}

    try:
        scraped = scrape_fundrich_tisa_list()
        scraped_funds = scraped.get("funds", [])
        holdings_as_of = scraped.get("holdings_as_of", "2026/10/31")
    except Exception as exc:
        print(f"[ERROR] 基富通頁面抓取失敗：{exc}")
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
                "tisa_type": "TISA專區",
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
