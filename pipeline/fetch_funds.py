import json
import os
import re
from datetime import datetime
from playwright.sync_api import sync_playwright

TISA_URL = "https://www.fundrich.com.tw/themeFund/promotionDetail?type=root.TISA.FundList_Web"
FUND_TABLE_API = "https://apis.fundrich.com.tw/FRSDataCenter/FundTableInfo"

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


def extract_funds_from_payload(data, results: dict):
    """遞迴掃描 FundTableInfo 回傳結構，擷取基金代碼、名稱、風險等級與其他資訊"""
    if isinstance(data, list):
        for item in data:
            extract_funds_from_payload(item, results)
    elif isinstance(data, dict):
        code = (
            data.get("fundId")
            or data.get("fundCode")
            or data.get("FundCode")
            or data.get("code")
            or ""
        )
        name = (
            data.get("fundName")
            or data.get("FundName")
            or data.get("name")
            or data.get("shortName")
            or ""
        )
        if isinstance(code, str) and isinstance(name, str):
            code_clean = code.strip().upper()
            name_clean = name.strip()
            if re.fullmatch(r"[A-Za-z0-9]{3,12}", code_clean) and ("基金" in name_clean or len(name_clean) >= 4):
                rr_raw = str(data.get("riskLevel") or data.get("rr") or data.get("risk") or "RR4").upper()
                rr = rr_raw if re.fullmatch(r"RR[1-5]", rr_raw) else (f"RR{rr_raw}" if re.fullmatch(r"[1-5]", rr_raw) else "RR4")
                issuer = (
                    data.get("companyName")
                    or data.get("issuerName")
                    or data.get("fundCompany")
                    or infer_issuer(name_clean)
                )
                results[code_clean] = {
                    "id": code_clean,
                    "name": name_clean,
                    "issuer": str(issuer).strip() or infer_issuer(name_clean),
                    "tisa_type": infer_tisa_type(name_clean),
                    "risk_level": rr,
                    "nav": data.get("nav") or data.get("netWorth") or data.get("price"),
                    "nav_date": data.get("navDate") or data.get("priceDate") or data.get("date"),
                    "currency": data.get("currency") or data.get("curr") or "",
                    "fund_size": data.get("fundSize") or data.get("size"),
                }
        for val in data.values():
            if isinstance(val, (dict, list)):
                extract_funds_from_payload(val, results)


def scrape_fundrich_tisa_list() -> dict:
    api_collected: dict[str, dict] = {}
    captured_template_payload = None
    captured_headers = None

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
            nonlocal captured_template_payload, captured_headers
            url = response.url
            if "FundTableInfo" in url:
                req = response.request
                if req.post_data and not captured_template_payload:
                    try:
                        captured_template_payload = json.loads(req.post_data)
                        captured_headers = {
                            k: v for k, v in req.headers.items()
                            if k.lower() in {"content-type", "accept", "authorization", "x-requested-with", "origin", "referer"}
                        }
                    except Exception:
                        pass
                try:
                    raw_txt = response.text()
                    if raw_txt.strip().startswith(("{", "[")):
                        extract_funds_from_payload(json.loads(raw_txt), api_collected)
                except Exception:
                    pass

        page.on("response", on_response)
        page.goto(TISA_URL, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_selector("fr-fund-table-row", timeout=25000)
        except Exception:
            pass
        page.wait_for_timeout(3000)

        base_payload = captured_template_payload or {
            "data": {
                "list": "root.TISA.FundList_Web",
                "currentPage": 1,
                "userstock": False,
            }
        }
        req_headers = captured_headers or {"Content-Type": "application/json"}

        # 依 currentPage 1..10 逐頁請求 FundTableInfo，直到抓滿或無新資料
        for page_num in range(1, 11):
            payload_copy = json.loads(json.dumps(base_payload))
            if isinstance(payload_copy.get("data"), dict):
                payload_copy["data"]["currentPage"] = page_num
            else:
                payload_copy["currentPage"] = page_num

            before_cnt = len(api_collected)
            try:
                resp_text = page.evaluate(
                    """async ({ url, headers, bodyStr }) => {
                        const res = await fetch(url, {
                            method: 'POST',
                            headers,
                            body: bodyStr
                        });
                        return await res.text();
                    }""",
                    {
                        "url": FUND_TABLE_API,
                        "headers": req_headers,
                        "bodyStr": json.dumps(payload_copy, ensure_ascii=False),
                    },
                )
                if resp_text and resp_text.strip().startswith(("{", "[")):
                    extract_funds_from_payload(json.loads(resp_text), api_collected)
            except Exception as e:
                print(f"[DEBUG] currentPage={page_num} 請求失敗：{e}")
                break

            added = len(api_collected) - before_cnt
            print(f"[DEBUG] currentPage={page_num} 新增 {added} 檔（累計 {len(api_collected)} 檔）")
            if page_num > 1 and added == 0:
                break

        body_text = page.inner_text("body")
        holdings_date_match = re.search(r"持股資料日[：:\s]*(\d{4}/\d{2}/\d{2})", body_text)
        holdings_as_of = holdings_date_match.group(1) if holdings_date_match else "2026/10/31"
        browser.close()

    return {
        "holdings_as_of": holdings_as_of,
        "funds": list(api_collected.values()),
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
