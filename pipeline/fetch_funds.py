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


def scrape_fundrich_tisa_list() -> dict:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="zh-TW",
        )
        page = context.new_page()
        page.goto(TISA_URL, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        page.wait_for_timeout(4000)

        # 1. 印出包含 SCH044 的外層 HTML 結構，找出基金名稱與代碼的真實標籤關係
        dom_probe = page.evaluate(
            """() => {
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                let node;
                while ((node = walker.nextNode())) {
                    if (node.nodeValue && node.nodeValue.includes('SCH044')) {
                        let parent = node.parentElement;
                        for (let i = 0; i < 4 && parent && parent.parentElement; i++) {
                            parent = parent.parentElement;
                        }
                        return parent ? parent.outerHTML.slice(0, 2500) : 'NO_PARENT';
                    }
                }
                return 'SCH044_NOT_IN_MAIN_BODY';
            }"""
        )
        print("=== DOM PROBE (SCH044 附近 HTML) ===")
        print(dom_probe)

        # 2. 印出頁面上所有分頁器、頁籤或按鈕的文字與 class
        nav_probe = page.evaluate(
            """() => {
                const els = Array.from(document.querySelectorAll('button, a, [role="tab"], [class*="page"], [class*="tab"], [class*="more"]'));
                return els
                    .map(el => ({
                        tag: el.tagName,
                        cls: (el.className || '').toString().slice(0, 60),
                        txt: (el.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 40)
                    }))
                    .filter(x => x.txt.length > 0 && x.txt.length < 30)
                    .slice(0, 50);
            }"""
        )
        print("=== NAV / PAGINATION PROBE ===")
        print(json.dumps(nav_probe, ensure_ascii=False))

        # 3. 嘗試透過純文字相鄰行配對（基富通通常一筆基金就是連續幾行：名稱 / 代碼 / 淨值 / 報酬率）
        all_text_parts = []
        for fr in page.frames:
            try:
                t = fr.inner_text("body")
                if t.strip():
                    all_text_parts.append(t)
            except Exception:
                pass

        combined_text = "\n".join(all_text_parts)
        lines = [line.strip() for line in combined_text.split("\n") if line.strip()]

        extracted_items = []
        seen_ids = set()
        for i, line in enumerate(lines):
            # 尋找獨立出現的基金代碼（或 "代碼：SCH044"）
            m = re.fullmatch(r"(?:代碼[：:]\s*)?([A-Z0-9]{3}[0-9]{2,3}[A-Z0-9]?)", line)
            if not m:
                continue
            fid = m.group(1).upper()
            if fid in seen_ids or fid in {"TISA", "RR1", "RR2", "RR3", "RR4", "RR5"}:
                continue
            seen_ids.add(fid)

            # 往前或往後找 3 行內包含「基金」字樣的完整名稱
            window = lines[max(0, i - 3) : min(len(lines), i + 4)]
            best_name = next((w for w in window if "基金" in w and len(w) >= 5), f"代碼：{fid}")
            context_str = " ".join(window)
            rr_m = re.search(r"RR[1-5]", context_str)

            extracted_items.append(
                {
                    "id": fid,
                    "name": best_name,
                    "issuer": infer_issuer(best_name, context_str),
                    "risk_level": rr_m.group(0) if rr_m else "RR4",
                }
            )

        holdings_date_match = re.search(r"持股資料日[：:\s]*(\d{4}/\d{2}/\d{2})", combined_text)
        holdings_as_of = holdings_date_match.group(1) if holdings_date_match else "2026/10/31"
        browser.close()

    return {
        "holdings_as_of": holdings_as_of,
        "funds": extracted_items,
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
