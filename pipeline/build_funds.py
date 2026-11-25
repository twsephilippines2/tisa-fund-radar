import json
import os
from datetime import datetime
from typing import Literal
import numpy as np
import pandas as pd
from google import genai
from google.genai import types
from pydantic import BaseModel, Field


# ==========================================
# 1. 定義 Gemini 結構化輸出 Schema（確保前端不破版）
# ==========================================
class FundAIDiagnosis(BaseModel):
    style_mismatch_flag: bool = Field(
        description="基金名稱與實際持股風格是否出現明顯落差（例如掛名高股息但科技股占比極高）"
    )
    mismatch_badge: str = Field(
        description="若 style_mismatch_flag 為 true，給出 6~10 字的警示標籤，如「名稱有股息・持股偏成長」；否則回傳「名實相符」"
    )
    plain_summary: str = Field(
        description="給投資小白看的 40~60 字白話診斷總結，點出這檔基金的真實性格、集中度風險或換股動向，嚴禁編造未提供的數字"
    )
    radar_action_note: str = Field(
        description="根據跨月持股與產業變化，用 25~40 字白話說明經理人近期是在追高科技、轉向防禦或維持原狀"
    )


# ==========================================
# 2. 確定性計算層（算數交給程式，絕不讓 AI 瞎猜）
# ==========================================
def calculate_overlap(fund_holdings: dict[str, float], etf_holdings: dict[str, float]) -> float:
    """計算基金與基準 ETF（如 0050、00878）的持股權重交集重疊率（%）"""
    common_stocks = set(fund_holdings.keys()) & set(etf_holdings.keys())
    overlap = sum(min(fund_holdings[s], etf_holdings[s]) for s in common_stocks)
    return round(overlap, 1)


def calculate_risk_metrics(nav_series: list[float], risk_free_rate: float = 0.015) -> dict:
    """根據歷史淨值序列計算最大回撤（Max Drawdown）、年化波動度與夏普值"""
    prices = np.array(nav_series, dtype=float)
    returns = np.diff(prices) / prices[:-1]

    # 最大回撤
    cum_max = np.maximum.accumulate(prices)
    drawdowns = (prices - cum_max) / cum_max
    max_dd = round(float(np.min(drawdowns)) * 100, 1)

    # 年化波動度與夏普值（假設日淨值 252 交易日）
    ann_vol = round(float(np.std(returns, ddof=1) * np.sqrt(252)) * 100, 1)
    ann_ret = float((prices[-1] / prices[0]) ** (252 / len(returns)) - 1)
    sharpe = round((ann_ret - risk_free_rate) / (ann_vol / 100), 2) if ann_vol > 0 else 0.0

    return {
        "max_drawdown_pct": max_dd,
        "annual_volatility_pct": ann_vol,
        "sharpe_ratio": sharpe,
    }


def calculate_holdings_diff(prev_holdings: dict[str, float], curr_holdings: dict[str, float]) -> dict:
    """比對前後兩期持股，抓出新增/剔除標與權重變化最大的個股"""
    added = [s for s in curr_holdings if s not in prev_holdings]
    removed = [s for s in prev_holdings if s not in curr_holdings]
    changes = []
    for stock in set(prev_holdings.keys()) | set(curr_holdings.keys()):
        diff = round(curr_holdings.get(stock, 0.0) - prev_holdings.get(stock, 0.0), 2)
        if abs(diff) >= 0.5:
            changes.append({"stock": stock, "diff_pct": diff})
    changes.sort(key=lambda x: abs(x["diff_pct"]), reverse=True)
    return {
        "added": added[:5],
        "removed": removed[:5],
        "top_weight_changes": changes[:5],
    }


# ==========================================
# 3. AI 語意摘要與反差偵測層（Gemini API）
# ==========================================
def generate_ai_insight(client: genai.Client, fund_metrics: dict) -> dict:
    prompt = f"""
你是一位專業且擅長用白話向投資新手解釋的基金分析師。
請根據以下「已由程式精確計算完成」的基金數據，產出結構化的白話診斷。
嚴格遵守規則：只能引用下列提供的數字，不可自行捏造報酬率或持股比例。

【基金資料】
- 基金名稱：{fund_metrics['name']}
- 名義分類：{fund_metrics['category']}
- 實際科技股占比：{fund_metrics['tech_exposure_pct']}%
- 台積電單一持股占比：{fund_metrics['tsmc_pct']}%
- 與 0050 重疊率：{fund_metrics['overlap_0050_pct']}%
- 與高股息 ETF (00878) 重疊率：{fund_metrics['overlap_00878_pct']}%
- 最大回撤：{fund_metrics['risk']['max_drawdown_pct']}%
- 跨月持股變動摘要：{json.dumps(fund_metrics['holdings_diff'], ensure_ascii=False)}
"""

    response = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=FundAIDiagnosis,
            temperature=0.2,  # 低溫度確保輸出穩定、不浮誇
        ),
    )
    return json.loads(response.text)


# ==========================================
# 4. 主流程組裝與輸出 funds.json
# ==========================================
def run_pipeline():
    # 需事先設定環境變數 GEMINI_API_KEY
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    # 範例輸入資料（實務上改為讀取爬蟲抓下的 raw_holdings_curr.json 與基準 ETF 成分股）
    etf_0050 = {"2330 台積電": 54.2, "2317 鴻海": 5.1, "2454 聯發科": 4.3, "2308 台達電": 2.1}
    etf_00878 = {"2382 廣達": 4.8, "3231 緯創": 4.2, "2881 富邦金": 4.5, "2882 國泰金": 4.1}

    sample_funds = [
        {
            "id": "YUI116",
            "name": "元大台灣高股息優質龍頭證券投資信託基金-新台幣R類型",
            "issuer": "元大投信",
            "tisa_type": "好好退休",
            "category": "台股主動型",
            "risk_level": "RR4",
            "ter_pct": 1.15,
            "rule_note": "定期定額滿24個月享0手續費與優惠經理費",
            "tech_exposure_pct": 89.0,
            "prev_holdings": {"2330 台積電": 9.8, "2454 聯發科": 7.5, "2382 廣達": 5.2, "2303 聯電": 3.1},
            "curr_holdings": {"2330 台積電": 11.4, "2454 聯發科": 8.6, "2308 台達電": 6.1, "3017 奇鋐": 4.2},
            "nav_history": [32.0, 34.5, 38.0, 31.2, 35.8, 37.22],
        }
    ]

    output_records = []
    for f in sample_funds:
        curr_h = f["curr_holdings"]
        metrics = {
            "id": f["id"],
            "name": f["name"],
            "issuer": f["issuer"],
            "tisa_type": f["tisa_type"],
            "category": f["category"],
            "risk_level": f["risk_level"],
            "ter_pct": f["ter_pct"],
            "rule_note": f["rule_note"],
            "tech_exposure_pct": f["tech_exposure_pct"],
            "tsmc_pct": curr_h.get("2330 台積電", 0.0),
            "overlap_0050_pct": calculate_overlap(curr_h, etf_0050),
            "overlap_00878_pct": calculate_overlap(curr_h, etf_00878),
            "risk": calculate_risk_metrics(f["nav_history"]),
            "holdings_diff": calculate_holdings_diff(f["prev_holdings"], curr_h),
            "top_holdings": [{"name": k, "weight": v} for k, v in curr_h.items()],
        }

        # 呼叫 Gemini 產出白話診斷與名稱反差標籤
        metrics["ai_diagnosis"] = generate_ai_insight(client, metrics)
        output_records.append(metrics)

    final_payload = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "holdings_as_of": "2026/10/31",
        "total_count": len(output_records),
        "funds": output_records,
    }

    os.makedirs("data", exist_ok=True)
    with open("data/funds.json", "w", encoding="utf-8") as fp:
        json.dump(final_payload, fp, ensure_ascii=False, indent=2)
    print("已成功產出 data/funds.json")


if __name__ == "__main__":
    run_pipeline()
