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


def pick_best_fund_name(lines: list[str], fund_id: str) -> str:
    # 優先挑出包含「基金」字樣的完整名稱
    for line in lines:
        cleaned = re.sub(r"^(代碼[：:]\s*[A-Z0-9]+|基金代號[：:]\s*[A-Z0-9]+)\s*", "", line).strip()
        if "基金" in cleaned and len(cleaned) >= 4:
            return cleaned
    for line in lines:
        cleaned = line.strip()
        if len(cleaned) >= 4 and not re.fullmatch(r"(代碼[：:]\s*)?[A-Z0-9]{4,8}", cleaned):
            return cleaned
    return f"代碼：{fund_id}"


def scrape_fundrich_tisa_list() -> dict:
    """使用無頭 Chromium 載入基富通 TISA 專區，捲動/展開後擷取基金清單"""
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

        # 嘗試分段捲動並點擊可能的「看更多／下一頁」按鈕
        for _ in range(8):
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass
            page.wait_for_timeout(1200)

            for btn_text in ["看更多", "顯示更多", "載入更多", "下一頁", "全部"]:
                try:
                    btn = page.get_by_role("button", name=re.compile(btn_text)).first
                    if btn.is_visible(timeout=400):
                        btn.click(timeout=1500)
                        page.wait_for_timeout(1200)
                except Exception:
                    pass

        all_text_parts = []
        extracted_items = []
        seen_ids = set()

        for idx, fr in enumerate(page.frames):
            try:
                fr_text = fr.inner_text("body")
            except Exception:
                fr_text = ""
            if fr_text.strip():
                all_text_parts.append(fr_text)

            try:
                rows = fr.evaluate(
                    """() => {
                        const results = [];
                        const candidates = Array.from(
                            document.querySelectorAll('tr, [role="row"], li, .fund-item, [class*="fund"], [class*="list"]')
                        );
                        for (const el of candidates) {
                            const txt = (el.innerText || '').trim();
                            if (!txt || txt.length < 5 || txt.length > 800) continue;

                            const link = el.querySelector('a[href]');
                            const href = link ? (link.getAttribute('href') || '') : '';
                            const hrefCode = href.match(/(?:fundCode=|code=|\\/fund\\/)([A-Z0-9]{4,10})/i);

                            // 常見台灣基金代號格式：3碼英數投信碼 + 3碼數字或英數（例如 SCH044、YUI116、0050 等）
                            const codeMatches = Array.from(txt.matchAll(/\\b([A-Z0-9]{3}[0-9]{2,3}[A-Z0-9]?)\\b/g)).map(m => m[1]);
                            const id = (hrefCode && hrefCode[1]) || (codeMatches.length ? codeMatches[0] : '');
                            if (!id) continue;

                            const upperId = id.toUpperCase();
                            if (['TISA', 'HTML', 'HTTP', 'HTTPS', 'RR1', 'RR2', 'RR3', 'RR4', 'RR5'].includes(upperId)) {
                                continue;
                            }

                            const lines = txt.split('\\n').map(s => s.trim()).filter(Boolean);
                            const rrMatch = txt.match(/RR[1-5]/);
                            results.push({
                                id: upperId,
                                lines,
                                fullText: txt,
                                risk_level: rrMatch ? rrMatch[0] : 'RR4'
                            });
                        }
                        return results;
                    }"""
                )

                for r in rows:
                    rid = r["id"]
                    if not rid or rid in seen_ids:
                        continue
                    seen_ids.add(rid)
                    best_name = pick_best_fund_name(r["lines"], rid)
                    extracted_items.append(
                        {
                            "id": rid,
                            "name": best_name,
                            "issuer": infer_issuer(best_name, r["fullText"]),
                            "risk_level": r["risk_level"],
                        }
                    )
            except Exception as exc:
                print(f"[DEBUG] Frame[{idx}] evaluate 失敗：{exc}")

        combined_text = "\n".join(all_text_parts)
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
