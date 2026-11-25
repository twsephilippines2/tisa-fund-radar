import json
import os
import time
import numpy as np
from datetime import datetime
from pydantic import BaseModel, Field
from google import genai
from google.genai import types


class FundDiagnosis(BaseModel):
    style_mismatch_flag: bool = Field(
        description="若基金名稱含「高股息/穩健/平衡」，但科技股占比>60%或前十大高度集中電子成長股，標記為 True"
    )
    mismatch_badge: str = Field(
        description="簡短警示標籤（6~12字），例如：'名稱有股息・持股偏成長' 或 '名實相符'"
    )
    plain_summary: str = Field(
        description="用白話文說明該基金實際持股集中度、與0050/00878重疊風險及波動度（限80字內）"
    )
    radar_action_note: str = Field(
        description="針對近期成分股增減變化的白話觀察（限50字內）"
    )


def calculate_overlap(fund_holdings: dict[str, float], etf_holdings: dict[str, float]) -> float:
    if not fund_holdings or not etf_holdings:
        return 0.0
    common_stocks = set(fund_holdings.keys()) & set(etf_holdings.keys())
    overlap = sum(min(fund_holdings[s], etf_holdings[s]) for s in common_stocks)
    return round(overlap, 2)


def calculate_risk_metrics(nav_series: list[float], periods_per_year: int = 252, rf_annual: float = 0.015) -> dict:
    if not nav_series or len(nav_series) < 2:
        return {
            "max_drawdown_pct": 0.0,
            "annual_volatility_pct": 0.0,
            "sharpe_ratio": 0.0,
        }
    nav = np.array(nav_series, dtype=float)
    returns = np.diff(nav) / nav[:-1]

    running_max = np.maximum.accumulate(nav)
    drawdowns = (nav - running_max) / running_max
    max_drawdown = float(np.min(drawdowns) * 100)

    ann_vol = float(np.std(returns, ddof=1) * np.sqrt(periods_per_year) * 100)
    ann_return = float(np.mean(returns) * periods_per_year)
    sharpe = float((ann_return - rf_annual) / (ann_vol / 100)) if ann_vol > 0 else 0.0

    return {
        "max_drawdown_pct": round(max_drawdown, 2),
        "annual_volatility_pct": round(ann_vol, 2),
        "sharpe_ratio": round(sharpe, 2),
    }


def calculate_holdings_diff(prev_h: dict[str, float], curr_h: dict[str, float]) -> dict:
    if not prev_h or not curr_h:
        return {"added": [], "removed": [], "top_weight_changes": []}
    prev_keys, curr_keys = set(prev_h.keys()), set(curr_h.keys())
    added = sorted(list(curr_keys - prev_keys))
    removed = sorted(list(prev_keys - curr_keys))

    changes = []
    for stock in prev_keys | curr_keys:
        diff = round(curr_h.get(stock, 0.0) - prev_h.get(stock, 0.0), 2)
        if abs(diff) >= 0.5:
            changes.append({"stock": stock, "diff_pct": diff})
    changes.sort(key=lambda x: abs(x["diff_pct"]), reverse=True)

    return {
        "added": added,
        "removed": removed,
        "top_weight_changes": changes[:5],
    }


def generate_ai_insight(client: genai.Client, fund_payload: dict) -> dict:
    # 若尚未取得持股明細，先不耗用 Gemini 配額
    if not fund_payload.get("top_holdings"):
        return {
            "style_mismatch_flag": False,
            "mismatch_badge": "已收錄清單",
            "plain_summary": f"已自基富通 TISA 專區收錄（風險等級 {fund_payload['risk_level']}），待補齊單檔持股與淨值明細後即可進行深度穿透診斷。",
            "radar_action_note": "等待補齊月報前十大持股明細。",
        }

    prompt = f"""
    你是台灣共同基金與 TISA 退休投資分析師。請根據以下真實量化指標，產出精準、客觀、無行銷話術的白話診斷：
    基金名稱：{fund_payload['name']}
    投資類型：{fund_payload['category']}
    科技股占比：{fund_payload['tech_exposure_pct']}%
    最大持股權重（台積電）：{fund_payload['tsmc_pct']}%
    與 0050 持股重疊度：{fund_payload['overlap_0050_pct']}%
    與 00878 持股重疊度：{fund_payload['overlap_00878_pct']}%
    最大回撤：{fund_payload['risk']['max_drawdown_pct']}%
    年化波動度：{fund_payload['risk']['annual_volatility_pct']}%
    近期新增持股：{fund_payload['holdings_diff']['added']}
    近期剔除持股：{fund_payload['holdings_diff']['removed']}
    """

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=FundDiagnosis,
                    temperature=0.2,
                ),
            )
            return json.loads(response.text)
        except Exception as exc:
            print(f"[WARN] {fund_payload['id']} 第 {attempt + 1} 次呼叫失敗：{exc}")
            if attempt < max_retries - 1:
                time.sleep(5 * (attempt + 1))

    return {
        "style_mismatch_flag": fund_payload["tech_exposure_pct"] > 60 and (
            "股息" in fund_payload["name"] or "平衡" in fund_payload["name"]
        ),
        "mismatch_badge": "名稱有股息・持股偏成長" if fund_payload["tech_exposure_pct"] > 60 else "AI暫忙",
        "plain_summary": f"科技股占比 {fund_payload['tech_exposure_pct']}%、台積電 {fund_payload['tsmc_pct']}%，最大回撤 {fund_payload['risk']['max_drawdown_pct']}%；AI 服務暫時忙碌，稍後重新執行即可更新完整診斷。",
        "radar_action_note": "AI 摘要暫時忙碌，建議稍後手動重跑或等待下次排程更新。",
    }


def run_pipeline():
    if not os.path.exists("data/raw_funds.json"):
        raise FileNotFoundError("找不到 data/raw_funds.json，請先執行 fetch_funds.py")

    with open("data/raw_funds.json", "r", encoding="utf-8") as fp:
        raw_data = json.load(fp)

    benchmarks = raw_data.get("etf_benchmarks", {})
    etf_0050 = benchmarks.get("0050", {})
    etf_00878 = benchmarks.get("00878", {})
    funds_list = raw_data.get("funds", [])

    api_key = os.environ.get("GEMINI_API_KEY", "")
    client = genai.Client(api_key=api_key) if api_key else None

    output_records = []
    for f in funds_list:
        curr_h = f.get("curr_holdings", {})
        prev_h = f.get("prev_holdings", {})
        metrics = {
            "id": f["id"],
            "name": f["name"],
            "issuer": f["issuer"],
            "tisa_type": f["tisa_type"],
            "category": f["category"],
            "risk_level": f["risk_level"],
            "ter_pct": f["ter_pct"],
            "rule_note": f["rule_note"],
            "tech_exposure_pct": f.get("tech_exposure_pct", 0.0),
            "tsmc_pct": curr_h.get("2330 台積電", 0.0),
            "overlap_0050_pct": calculate_overlap(curr_h, etf_0050),
            "overlap_00878_pct": calculate_overlap(curr_h, etf_00878),
            "risk": calculate_risk_metrics(
                f.get("nav_history", []),
                periods_per_year=f.get("periods_per_year", 252),
            ),
            "holdings_diff": calculate_holdings_diff(prev_h, curr_h),
            "top_holdings": [{"name": k, "weight": v} for k, v in curr_h.items()],
        }

        if client:
            metrics["ai_diagnosis"] = generate_ai_insight(client, metrics)
            if metrics["top_holdings"]:
                time.sleep(1.5)
        else:
            metrics["ai_diagnosis"] = {
                "style_mismatch_flag": False,
                "mismatch_badge": "未設定API金鑰",
                "plain_summary": "未偵測到 GEMINI_API_KEY。",
                "radar_action_note": "-",
            }

        output_records.append(metrics)

    final_payload = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "holdings_as_of": raw_data.get("holdings_as_of", "2026/10/31"),
        "source": raw_data.get("source", "基富通 FundRich TISA 專區"),
        "total_count": len(output_records),
        "funds": output_records,
    }

    os.makedirs("data", exist_ok=True)
    with open("data/funds.json", "w", encoding="utf-8") as fp:
        json.dump(final_payload, fp, ensure_ascii=False, indent=2)
    print(f"已成功產出 data/funds.json，共 {len(output_records)} 檔基金")


if __name__ == "__main__":
    run_pipeline()
