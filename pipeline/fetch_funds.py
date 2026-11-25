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


def load_json_if_exists(path: str, default: dict) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def scrape_fundrich_tisa_list() -> dict:
    """使用無頭 Chromium 載入基富通 TISA 專區，同時攔截 JSON API 與掃描所有 Frame"""
    captured_apis = []

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

        def handle_response(response):
            content_type = response.headers.get("content-type", "")
            if "application/json" in content_type or "text/json" in content_type:
                try:
                    text = response.text()
                    if any(k in text for k in ["TISA", "fundCode", "FundCode", "基金", "SCH044", "YUI116"]):
                        captured_apis.append(
                            {
                                "url": response.url,
                                "status": response.status,
                                "body_preview": text[:4000],
                            }
                        )
                except Exception:
                    pass

        page.on("response", handle_response)
        page.goto(TISA_URL, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        page.wait_for_timeout(5000)

        # 收集所有 frame 文字與候選基金列
        all_text_parts = []
        extracted_items = []
        seen_ids = set()

        print(f"[DEBUG] Frame 數量：{len(page.frames)}，攔截到相關 JSON API 數：{len(captured_apis)}")
        for idx, fr in enumerate(page.frames):
            try:
                fr_text = fr.inner_text("body")
            except Exception:
                fr_text = ""
            if fr_text.strip():
                all_text_parts.append(fr_text)
                print(f"[DEBUG] Frame[{idx}] url={fr.url} 文字長度={len(fr_text)}")

            try:
                rows = fr.evaluate(
                    """() => {
                        const results = [];
                        const candidates = Array.from(document.querySelectorAll('tr, [role="row"], li, .fund-item, [class*="fund"]'));
                        for (const el of candidates) {
                            const txt = (el.innerText || '').trim();
                            if (!txt || txt.length < 6 || txt.length > 600) continue;
                            // 尋找疑似基金代碼（如 SCH044、YUI116 等 5~8 碼英數混合）
                            const codeMatch = txt.match(/\\b([A-Z]{2,5}[0-9A-Z]{2,5})\\b/);
                            const link = el.querySelector('a[href]');
                            const href = link ? (link.getAttribute('href') || '') : '';
                            const hrefCode = href.match(/(?:fundCode=|code=|\\/fund\\/)([A-Z0-9]{4,10})/i);
                            const id = (hrefCode && hrefCode[1]) || (codeMatch && codeMatch[1]) || '';
                            if (!id) continue;

                            const lines = txt.split('\\n').map(s => s.trim()).filter(Boolean);
                            const nameLine = lines.find(l => l.includes('基金') || l.includes('TISA')) || lines[0] || id;
                            const rrMatch = txt.match(/RR[1-5]/);
                            results.push({
                                id: id.toUpperCase(),
                                name: nameLine,
                                risk_level: rrMatch ? rrMatch[0] : 'RR4',
                                href,
                                snippet: txt.slice(0, 180)
                            });
                        }
                        return results;
                    }"""
                )
                for r in rows:
                    rid = r["id"]
                    if rid and rid not in seen_ids:
                        seen_ids.add(rid)
                        extracted_items.append(
                            {
                                "id": rid,
                                "name": r["name"],
                                "issuer": "TISA合作投信",
                                "risk_level": r["risk_level"],
                            }
                        )
            except Exception as exc:
                print(f"[DEBUG] Frame[{idx}] evaluate 失敗：{exc}")

        combined_text = "\n".join(all_text_parts)
        holdings_date_match = re.search(r"持股資料日[：:\s]*(\d{4}/\d{2}/\d{2})", combined_text)
        holdings_as_of = holdings_date_match.group(1) if holdings_date_match else "2026/10/31"

        browser.close()

    os.makedirs("data", exist_ok=True)
    with open("data/debug_api_responses.json", "w", encoding="utf-8") as dbg:
        json.dump(captured_apis[:15], dbg, ensure_ascii=False, indent=2)

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
