import streamlit as st
import pandas as pd
import numpy as np
from io import BytesIO
import base64
import zipfile
import importlib.util

# ============================================================
# IBR Pricing Simulator v6.6 (Multi-policy + Market Anchors)
#
# Goals
# - Cost-only engine: (1) 손해 안 보는 최소선(Min) + (2) 채널 서열 밴드 구조 생성
# - Market-anchor extension: MSRP/상시의 출발점을 "시장 앵커"로 정하고,
#   그 위에 손익 하한/서열 안전장치를 얹는 구조
# - Multiple pricing policies: 같은 SKU에 대해 여러 정책 결과를 동시에 비교
#
# Key concepts (non-technical)
# - "Min(최저가)": 이 가격보다 싸게 팔면 '원가/배송/수수료' 때문에 손해를 보기 쉬움
# - "Max(최고가/MSRP)": 채널별 밴드를 만드는 '상단 앵커' (정가 프레이밍)
# - "Band": Min~Max 사이를 가격영역(공구/홈쇼핑/상시...) 순서대로 구간 분할한 것
# - "Floor(손익하한)": 해당 채널 비용구조(수수료/배송/반품/마케팅)에 맞춰
#   손해를 피하기 위한 최소가격
#
# Dependencies
# - streamlit, pandas, numpy (no openpyxl required)
# - Template download uses prebuilt base64 xlsx
# ============================================================

st.set_page_config(page_title="IBR Pricing Simulator v7", layout="wide")

# -----------------------------
# Global constants
# -----------------------------
PRICE_ZONES = ["공구", "홈쇼핑", "폐쇄몰", "모바일라방", "원데이", "브랜드위크", "홈사", "상시", "오프라인", "MSRP"]

DEFAULT_CHANNELS = [
    ("오프라인",     0.50, 0.00, 0.0,    0.00, 0.00, 0.0),
    ("자사몰",       0.05, 0.03, 3000.0, 0.00, 0.00, 0.0),
    ("스마트스토어", 0.05, 0.03, 3000.0, 0.00, 0.00, 0.0),
    ("쿠팡",         0.25, 0.00, 3000.0, 0.00, 0.00, 0.0),
    ("오픈마켓",     0.15, 0.00, 3000.0, 0.00, 0.00, 0.0),
    ("홈사",         0.30, 0.03, 3000.0, 0.00, 0.00, 0.0),
    ("공구",         0.50, 0.03, 3000.0, 0.00, 0.00, 0.0),
    ("홈쇼핑",       0.55, 0.00, 0.0,    0.00, 0.00, 0.0),
    ("모바일라이브", 0.40, 0.03, 3000.0, 0.00, 0.00, 0.0),
    ("폐쇄몰",       0.25, 0.00, 3000.0, 0.00, 0.00, 0.0),
]

DEFAULT_ZONE_MAP = {
    "공구": "공구",
    "홈쇼핑": "홈쇼핑",
    "폐쇄몰": "폐쇄몰",
    "모바일라방": "모바일라이브",
    "원데이": "자사몰",
    "브랜드위크": "자사몰",
    "홈사": "홈사",
    "상시": "자사몰",
    "오프라인": "오프라인",
    "MSRP": "자사몰",
}

DEFAULT_BOUNDARIES = [0, 10, 20, 30, 42, 52, 62, 72, 84, 94, 100]  # len=11 (10 zones)

def default_zone_target_pos(boundaries):
    # 기본: 각 밴드 중앙
    return {z: (boundaries[i] + boundaries[i+1]) / 2 for i, z in enumerate(PRICE_ZONES)}

# -----------------------------
# Policies (multi)
# -----------------------------
# Explanation:
# - cost_only: MSRP는 원가 기반(상시 목표 원가율/상시할인율)로 추정
# - market_anchor: MSRP는 시장앵커(동일상품/경쟁카테고리)에서 시작
# - hybrid_max: MSRP는 cost_only와 market_anchor 중 더 큰 값(보수적)
# - hybrid_weighted: MSRP는 두 앵커를 가중 평균 (정책 파라미터로 조정)
#
# Note: 모든 정책은 'Min은 최저가 원가율 상한 + 손익하한'을 기반으로 잡는다.
POLICIES = [
    {"policy": "cost_only", "설명": "원가 기반(상시 목표 원가율/상시할인율)만 사용", "w_market": 0.0, "take_max": False},
    {"policy": "market_anchor", "설명": "시장 앵커(동일상품/경쟁카테고리) 중심", "w_market": 1.0, "take_max": False},
    {"policy": "hybrid_max", "설명": "원가 기반 vs 시장 앵커 중 더 큰 MSRP 선택(보수적)", "w_market": 0.5, "take_max": True},
    {"policy": "hybrid_weighted", "설명": "원가 기반과 시장 앵커를 가중 평균", "w_market": 0.6, "take_max": False},
]

MARKET_TYPES = ["미입력", "국내신상품(시장가없음)", "해외소싱-동일상품있음"]

# New constants for v7
CURRENCIES = [
    {"code": "USD", "symbol": "$", "name": "미국 달러", "default_rate": 1400},
    {"code": "EUR", "symbol": "€", "name": "유로", "default_rate": 1550},
    {"code": "GBP", "symbol": "£", "name": "영국 파운드", "default_rate": 1800},
    {"code": "AUD", "symbol": "A$", "name": "호주 달러", "default_rate": 920},
    {"code": "NZD", "symbol": "NZ$", "name": "뉴질랜드 달러", "default_rate": 850},
    {"code": "JPY", "symbol": "¥", "name": "일본 엔", "default_rate": 9.5},
    {"code": "CHF", "symbol": "CHF", "name": "스위스 프랑", "default_rate": 1600},
]

CATEGORY_TARGETS = {
    "꿀": {"target": 0.25, "max": 0.30},
    "식품": {"target": 0.25, "max": 0.30},
    "이너뷰티": {"target": 0.25, "max": 0.30},
    "뷰티": {"target": 0.25, "max": 0.30},
    "헬스": {"target": 0.30, "max": 0.35},
    "가구(풀마케팅)": {"target": 0.26, "max": 0.35},
    "가구(홈쇼핑only)": {"target": 0.35, "max": 0.40},
}

REVERSE_TIERS = [
    {"rate": 0.25, "label": "최선", "desc": "즉시 진행"},
    {"rate": 0.30, "label": "진행 가능", "desc": "표준 조건"},
    {"rate": 0.35, "label": "논의 필요", "desc": "채널 축소 검토"},
    {"rate": 0.40, "label": "드랍", "desc": "진행 불가"},
]

BULK_CATEGORIES = list(CATEGORY_TARGETS.keys())
RRP_METHODS = ["직접입력", "국내공식가", "해외공식가×1.1", "경쟁사조사역산"]


# -----------------------------
# Set Pricing Defaults (kept for continuity)
# -----------------------------
GIFT_KEYWORDS = ["쇼핑백", "트레이", "틴케이스", "스푼", "선물", "기프트", "포장", "케이스"]
SET_TYPES = ["multi", "assort", "gift"]

def make_default_set_disc_df():
    rows = []
    defaults = {
        "multi": {
            "공구": 45, "홈쇼핑": 55, "폐쇄몰": 35, "모바일라방": 40, "원데이": 30,
            "브랜드위크": 25, "홈사": 25, "상시": 3, "오프라인": 0, "MSRP": 0
        },
        "assort": {
            "공구": 42, "홈쇼핑": 55, "폐쇄몰": 33, "모바일라방": 38, "원데이": 28,
            "브랜드위크": 23, "홈사": 23, "상시": 10, "오프라인": 5, "MSRP": 0
        },
        "gift": {
            "공구": 45, "홈쇼핑": 58, "폐쇄몰": 35, "모바일라방": 40, "원데이": 30,
            "브랜드위크": 25, "홈사": 25, "상시": 12, "오프라인": 5, "MSRP": 0
        },
    }
    for stype in SET_TYPES:
        for z in PRICE_ZONES:
            rows.append({"세트타입": stype, "가격영역": z, "할인율(%)": defaults[stype].get(z, 0)})
    return pd.DataFrame(rows)

DEFAULT_SET_PARAMS = {
    "k_msrp_set_multi": 1.00,
    "k_msrp_set_assort": 0.98,
    "k_msrp_set_gift": 1.03,
    "pack_cost_default": 0.0,
    "pack_cost_gift": 700.0,
    "disc_pack_step_pct": 2.0,   # add = step * log2(pack_units)
    "disc_pack_cap_pct": 6.0,
}

# -----------------------------
# Utilities
# -----------------------------
def safe_float(x, default=np.nan):
    try:
        if x is None:
            return default
        if isinstance(x, str):
            s = x.strip()
            if s == "" or s == "-":
                return default
            s = s.replace(",", "")
            return float(s)
        v = float(x)
        if np.isnan(v) or np.isinf(v):
            return default
        return v
    except Exception:
        return default

def krw_round(x, unit=100):
    try:
        return int(round(float(x) / unit) * unit)
    except Exception:
        return 0

def krw_ceil(x, unit=100):
    try:
        x = float(x)
        return int(np.ceil(x / unit) * unit)
    except Exception:
        return 0

def _pick_excel_engine():
    """Return a pandas ExcelWriter engine that exists in the runtime, else None."""
    for eng, mod in [("openpyxl", "openpyxl"), ("xlsxwriter", "xlsxwriter")]:
        try:
            if importlib.util.find_spec(mod) is not None:
                return eng
        except Exception:
            continue
    return None

def to_excel_bytes(df_dict):
    """
    Returns: (bytes, ext, mime)
    - If an Excel engine exists (openpyxl or xlsxwriter): create .xlsx
    - Else: create .zip of UTF-8-SIG CSVs (one file per sheet)
    """
    eng = _pick_excel_engine()
    if eng is not None:
        bio = BytesIO()
        with pd.ExcelWriter(bio, engine=eng) as writer:
            for sh, df in df_dict.items():
                df.to_excel(writer, index=False, sheet_name=str(sh)[:31])
        return bio.getvalue(), "xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    bio = BytesIO()
    with zipfile.ZipFile(bio, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for sh, df in df_dict.items():
            csv = df.to_csv(index=False, encoding="utf-8-sig")
            zf.writestr(f"{str(sh)[:31]}.csv", csv)
    return bio.getvalue(), "zip", "application/zip"

# -----------------------------
# Excel template (cost master upload)
# -----------------------------
# Prebuilt .xlsx (base64-embedded). Works without openpyxl/xlsxwriter.
_COST_TEMPLATE_B64 = """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"""

def make_cost_master_template_bytes():
    return base64.b64decode(_COST_TEMPLATE_B64.encode("ascii"))

# -----------------------------
# Load cost master
# -----------------------------
def find_cost_sheet(xls: pd.ExcelFile):
    candidates = []
    for sh in xls.sheet_names:
        try:
            df = pd.read_excel(xls, sheet_name=sh, header=2)
            cols = [str(c).strip() for c in df.columns]
            colset = set(cols)
            if ("상품코드" in colset) and any(c in colset for c in ["원가 (vat-)", "원가", "매입원가", "랜디드코스트", "랜디드코스트(총원가)"]):
                candidates.append(sh)
        except Exception:
            continue
    return candidates[0] if candidates else xls.sheet_names[0]

def load_products_from_cost_master(file) -> pd.DataFrame:
    xls = pd.ExcelFile(file)
    sh = find_cost_sheet(xls)
    df = pd.read_excel(xls, sheet_name=sh, header=2)
    df.columns = [str(c).strip() for c in df.columns]

    code_col = "상품코드" if "상품코드" in df.columns else None
    name_col = "상품명" if "상품명" in df.columns else None
    brand_col = "브랜드" if "브랜드" in df.columns else None

    cost_col = None
    for c in ["원가 (vat-)", "원가", "매입원가", "랜디드코스트", "랜디드코스트(총원가)"]:
        if c in df.columns:
            cost_col = c
            break

    out = pd.DataFrame({
        "품번": df[code_col].astype(str).str.strip() if code_col else "",
        "상품명": df[name_col].astype(str).str.strip() if name_col else "",
        "브랜드": df[brand_col].astype(str).str.strip() if brand_col else "",
        "원가": pd.to_numeric(df[cost_col], errors="coerce") if cost_col else np.nan,
    })
    out = out[out["품번"].ne("")].drop_duplicates(subset=["품번"]).reset_index(drop=True)

    # per-SKU overrides
    out["MSRP_오버라이드"] = np.nan
    out["Min_오버라이드"] = np.nan
    out["Max_오버라이드"] = np.nan
    out["운영여부"] = True
    return out

# -----------------------------
# Economics
# -----------------------------
def floor_price(cost_total, q_orders, fee, pg, mkt, ship_per_order, ret_rate, ret_cost_order, min_cm):
    denom = 1.0 - (fee + pg + mkt + min_cm)
    if denom <= 0:
        return float("inf")
    ship_unit = ship_per_order / max(1, q_orders)
    ret_unit = (ret_rate * ret_cost_order) / max(1, q_orders)
    return (cost_total + ship_unit + ret_unit) / denom

def contrib_metrics(price, cost_total, q_orders, fee, pg, mkt, ship_per_order, ret_rate, ret_cost_order):
    if price <= 0:
        return np.nan, np.nan
    ship_unit = ship_per_order / max(1, q_orders)
    ret_unit = (ret_rate * ret_cost_order) / max(1, q_orders)
    net = price * (1.0 - fee - pg - mkt) - ship_unit - ret_unit - cost_total
    return net, net / price

# -----------------------------
# Market anchor math (easy-to-explain)
# -----------------------------
def market_anchor_msrp(
    mtype: str,
    same_sku_price: float,
    comp_price: float,
    comp_mult: float,
    blend_w_same: float,
    rounding_unit: int
):
    """
    Market MSRP anchor (how to interpret)
    - 국내 신상품: 동일상품 시장가가 없으니, '경쟁카테고리 대표가격'을 앵커로 씀
      => msrp_market = comp_price * comp_mult
    - 해외소싱 동일상품: 동일상품 시장가 + 경쟁카테고리(포지셔닝) 둘 다 고려
      => msrp_market = w * same_sku_price + (1-w) * (comp_price*comp_mult)
    - 미입력: NaN
    """
    mtype = str(mtype or "미입력")
    same_sku_price = safe_float(same_sku_price, np.nan)
    comp_price = safe_float(comp_price, np.nan)
    comp_mult = float(comp_mult) if comp_mult == comp_mult else 1.0
    blend_w_same = float(blend_w_same) if blend_w_same == blend_w_same else 0.7
    blend_w_same = min(max(blend_w_same, 0.0), 1.0)

    if mtype == "국내신상품(시장가없음)":
        if comp_price != comp_price or comp_price <= 0:
            return np.nan
        return krw_ceil(comp_price * comp_mult, rounding_unit)

    if mtype == "해외소싱-동일상품있음":
        a = same_sku_price if (same_sku_price == same_sku_price and same_sku_price > 0) else np.nan
        b = (comp_price * comp_mult) if (comp_price == comp_price and comp_price > 0) else np.nan
        if a != a and b != b:
            return np.nan
        if a != a:  # only b
            return krw_ceil(b, rounding_unit)
        if b != b:  # only a
            return krw_ceil(a, rounding_unit)
        return krw_ceil(blend_w_same * a + (1.0 - blend_w_same) * b, rounding_unit)

    return np.nan

# -----------------------------
# Auto range from cost (+ market anchors + multi policy)
# -----------------------------
def compute_auto_range(
    cost_total: float,
    channels_df: pd.DataFrame,
    zone_map: dict,
    boundaries: list,
    rounding_unit: int,
    min_cm: float,
    min_cost_ratio_cap: float,         # 예: 0.30  (최저가 원가율 상한)
    always_cost_ratio_target: float,   # 예: 0.18  (상시 목표 원가율)
    always_list_disc: float,           # 예: 0.20  (상시할인율: MSRP 대비)
    policy: str,
    market_row: dict | None,
    include_zones: list,
    min_zone: str = "공구",
    msrp_override=np.nan,
    min_override=np.nan,
    max_override=np.nan,
):
    """
    정책 핵심 (v6.6):
    - Min(최저가)는 "원가율 30% 상한" + "채널 손익하한(Floor)" 중 더 높은 값을 채택
      => Min = max( Floor(min_zone), cost / min_cost_ratio_cap )
    - MSRP/Max는 "원가 기반 추정"과 "시장 앵커"를 정책에 따라 결합
      => Max = f(policy, msrp_cost, msrp_market, overrides, channel-floor constraints)
    """

    ch_map = channels_df.set_index("채널명").to_dict("index")

    def zone_floor(z):
        ch = zone_map.get(z, "자사몰")
        p = ch_map.get(ch, None)
        if p is None:
            return np.nan
        return floor_price(
            cost_total=cost_total,
            q_orders=1,
            fee=float(p["수수료율"]),
            pg=float(p["PG"]),
            mkt=float(p["마케팅비"]),
            ship_per_order=float(p["배송비(주문당)"]),
            ret_rate=float(p["반품률"]),
            ret_cost_order=float(p["반품비(주문당)"]),
            min_cm=min_cm,
        )

    # -----------------
    # 1) Min (최저가)
    # -----------------
    min_floor = zone_floor(min_zone)

    min_ratio = np.nan
    if cost_total == cost_total and cost_total > 0 and min_cost_ratio_cap and float(min_cost_ratio_cap) > 0:
        min_ratio = float(cost_total) / float(min_cost_ratio_cap)

    if min_override == min_override and float(min_override) > 0:
        min_auto = float(min_override)
        min_note = "Min 오버라이드 적용"
    else:
        candidates = []
        if min_floor == min_floor and min_floor > 0:
            candidates.append(float(min_floor))
        if min_ratio == min_ratio and min_ratio > 0:
            candidates.append(float(min_ratio))
        if not candidates:
            return np.nan, np.nan, {"note": "Min 산출 불가: 원가/채널 파라미터를 확인하세요."}
        min_auto = max(candidates)
        min_note = "Min = max(손익하한, 원가/원가율상한)"

    min_auto = krw_ceil(min_auto, rounding_unit)

    # -----------------
    # 2) MSRP candidates
    # -----------------
    # (A) cost-only msrp
    msrp_cost = np.nan
    always_target = np.nan
    if cost_total == cost_total and cost_total > 0 and always_cost_ratio_target and float(always_cost_ratio_target) > 0:
        always_target = float(cost_total) / float(always_cost_ratio_target)
        always_target = krw_ceil(always_target, rounding_unit)
        disc = float(always_list_disc or 0.0)
        disc = min(max(disc, 0.0), 0.95)
        msrp_cost = float(always_target) / (1.0 - disc)
        msrp_cost = krw_ceil(msrp_cost, rounding_unit)

    # (B) market msrp
    msrp_market = np.nan
    if market_row:
        msrp_market = market_anchor_msrp(
            mtype=market_row.get("시장유형", "미입력"),
            same_sku_price=market_row.get("동일상품_시장가", np.nan),
            comp_price=market_row.get("경쟁카테고리_앵커가", np.nan),
            comp_mult=market_row.get("경쟁앵커_배수", 1.0),
            blend_w_same=market_row.get("동일상품_가중치", 0.7),
            rounding_unit=rounding_unit,
        )
        # direct override inside market table (optional)
        mo = safe_float(market_row.get("MSRP_시장오버라이드", np.nan), np.nan)
        if mo == mo and mo > 0:
            msrp_market = krw_ceil(mo, rounding_unit)

    # (C) apply policy
    pol = next((p for p in POLICIES if p["policy"] == policy), None)
    if pol is None:
        pol = POLICIES[0]

    msrp_policy = np.nan
    note_policy = f"정책={pol['policy']}"
    if pol.get("take_max", False):
        cand = []
        if msrp_cost == msrp_cost and msrp_cost > 0: cand.append(msrp_cost)
        if msrp_market == msrp_market and msrp_market > 0: cand.append(msrp_market)
        msrp_policy = max(cand) if cand else np.nan
        note_policy += " / MSRP = max(cost, market)"
    else:
        w = float(pol.get("w_market", 0.0))
        w = min(max(w, 0.0), 1.0)
        a = msrp_cost if (msrp_cost == msrp_cost and msrp_cost > 0) else np.nan
        b = msrp_market if (msrp_market == msrp_market and msrp_market > 0) else np.nan
        if a != a and b != b:
            msrp_policy = np.nan
        elif a != a:
            msrp_policy = b
            note_policy += " / MSRP = market (cost missing)"
        elif b != b:
            msrp_policy = a
            note_policy += " / MSRP = cost (market missing)"
        else:
            msrp_policy = (1.0 - w) * a + w * b
            note_policy += f" / MSRP = (1-w)*cost + w*market (w={w:.2f})"

    if msrp_policy == msrp_policy and msrp_policy > 0:
        msrp_policy = krw_ceil(msrp_policy, rounding_unit)

    # -----------------
    # 3) Ensure channel floors can fit in bands (optional but recommended)
    # - If some zone floor is above its BandHigh, increase Max so that BandHigh rises.
    # -----------------
    max_req = []
    span_dummy = max(1.0, (msrp_policy - min_auto) if (msrp_policy == msrp_policy and msrp_policy > min_auto) else 1.0)

    for i, z in enumerate(PRICE_ZONES):
        if z not in include_zones:
            continue
        fz = zone_floor(z)
        if fz != fz or fz <= 0:
            continue
        end = boundaries[i+1] / 100.0
        if end <= 0:
            continue
        # BandHigh = Min + (Max-Min)*end  -> need BandHigh >= fz
        # => Max >= Min + (fz-Min)/end
        if fz > min_auto:
            max_needed = min_auto + (fz - min_auto) / end
            max_req.append(max_needed)

    # -----------------
    # 4) Final Max/MSRP
    # -----------------
    candidates = []
    if msrp_policy == msrp_policy and msrp_policy > 0:
        candidates.append(msrp_policy)
    if msrp_cost == msrp_cost and msrp_cost > 0:
        candidates.append(msrp_cost)  # keep in candidate list (for diagnostics)
    if msrp_market == msrp_market and msrp_market > 0:
        candidates.append(msrp_market)
    if max_req:
        candidates.append(float(max(max_req)))
    if max_override == max_override and float(max_override) > 0:
        candidates.append(float(max_override))
    if msrp_override == msrp_override and float(msrp_override) > 0:
        candidates.append(float(msrp_override))

    if not candidates:
        max_auto = min_auto + rounding_unit * 20
        max_note = "MSRP 산출 불가 → 임시 스팬 부여"
    else:
        max_auto = float(max(candidates))
        max_note = "MSRP = max(정책 앵커, 채널Floor충족, 오버라이드)"

    max_auto = krw_ceil(max_auto, rounding_unit)
    if max_auto <= min_auto:
        max_auto = krw_ceil(min_auto + max(rounding_unit * 20, int(min_auto * 0.2)), rounding_unit)

    note = f"{min_note} / {max_note} / {note_policy}"
    if max_req and (msrp_policy == msrp_policy) and max_auto > msrp_policy:
        note += " (채널 손익하한 충족 위해 MSRP 상향 포함)"

    meta = {
        "note": note,
        "min_floor": float(min_floor) if min_floor == min_floor else np.nan,
        "min_ratio": float(min_ratio) if min_ratio == min_ratio else np.nan,
        "always_target": float(always_target) if always_target == always_target else np.nan,
        "msrp_cost": float(msrp_cost) if msrp_cost == msrp_cost else np.nan,
        "msrp_market": float(msrp_market) if msrp_market == msrp_market else np.nan,
        "msrp_policy": float(msrp_policy) if msrp_policy == msrp_policy else np.nan,
    }
    return float(min_auto), float(max_auto), meta

def build_zone_table(
    cost_total: float, min_price: float, max_price: float,
    channels_df: pd.DataFrame, zone_map: dict, boundaries: list, target_pos: dict,
    rounding_unit: int, min_cm: float, overrides_df: pd.DataFrame,
    item_type: str, item_id: str
):
    ch_map = channels_df.set_index("채널명").to_dict("index")
    if min_price != min_price or max_price != max_price or max_price <= min_price:
        return pd.DataFrame(columns=[
            '가격영역', '비용채널', 'BandLow', 'BandHigh', 'Floor(손익하한)',
            '추천가(Target)', '가격_오버라이드(원)', '최종가격(원)',
            '상태', '경고', '마진룸(원)=최종-Floor', '기여이익(원)', '기여이익률(%)'
        ])

    rows = []
    span = max_price - min_price
    for i, z in enumerate(PRICE_ZONES):
        start = boundaries[i] / 100.0
        end = boundaries[i+1] / 100.0
        band_low = min_price + span * start
        band_high = min_price + span * end
        pos = target_pos.get(z, (boundaries[i]+boundaries[i+1])/2) / 100.0
        target_raw = min_price + span * pos

        ch = zone_map.get(z, "자사몰")
        p = ch_map.get(ch, None)
        if p is None:
            continue

        floor = floor_price(cost_total, 1, p["수수료율"], p["PG"], p["마케팅비"], p["배송비(주문당)"], p["반품률"], p["반품비(주문당)"], min_cm)

        # Target is the recommended point inside band, but never below floor
        status = "OK"
        target = max(target_raw, floor)
        if floor > band_high:
            status = "불가(Floor>BandHigh)"
            target = band_high
        elif target > band_high:
            status = "조정(Target→BandHigh)"
            target = band_high

        ov = overrides_df[(overrides_df["오퍼타입"]==item_type) & (overrides_df["오퍼ID"]==item_id) & (overrides_df["가격영역"]==z)]
        override_price = safe_float(ov.iloc[0]["가격_오버라이드"], np.nan) if not ov.empty else np.nan
        effective = override_price if (override_price == override_price and override_price > 0) else target

        band_low_r = krw_round(band_low, rounding_unit)
        band_high_r = krw_round(band_high, rounding_unit)
        floor_r = krw_round(floor, rounding_unit)
        target_r = krw_round(target, rounding_unit)
        eff_r = krw_round(effective, rounding_unit) if (effective == effective and effective > 0) else np.nan

        cm, cmr = contrib_metrics(eff_r if eff_r==eff_r else 0, cost_total, 1, p["수수료율"], p["PG"], p["마케팅비"], p["배송비(주문당)"], p["반품률"], p["반품비(주문당)"])

        flags = []
        if eff_r == eff_r and eff_r < floor_r: flags.append("⚠️ 손익하한 미만")
        if eff_r == eff_r and eff_r < band_low_r: flags.append("⚠️ 밴드하한 미만")
        if eff_r == eff_r and eff_r > band_high_r and z != "MSRP": flags.append("⚠️ 밴드상한 초과")

        rows.append({
            "가격영역": z,
            "비용채널": ch,
            "BandLow": band_low_r,
            "BandHigh": band_high_r,
            "Floor(손익하한)": floor_r,
            "추천가(Target)": target_r,
            "가격_오버라이드(원)": (krw_round(override_price, rounding_unit) if override_price == override_price else np.nan),
            "최종가격(원)": eff_r,
            "상태": status,
            "경고": " / ".join(flags),
            "마진룸(원)=최종-Floor": (eff_r - floor_r) if (eff_r == eff_r) else np.nan,
            "기여이익(원)": int(round(cm)) if cm == cm else np.nan,
            "기여이익률(%)": round(cmr*100, 1) if cmr == cmr else np.nan,
        })
    return pd.DataFrame(rows)

# -----------------------------
# Set helpers (minimal, for compatibility)
# -----------------------------
def is_accessory_sku(sku: str, name: str, cost: float) -> bool:
    sku = str(sku or "")
    name = str(name or "")
    if sku.upper().startswith("U"):
        return True
    for kw in GIFT_KEYWORDS:
        if kw in name:
            return True
    try:
        if float(cost) > 0 and float(cost) <= 800:
            return True
    except Exception:
        pass
    return False

def compute_set_cost(set_id: str, bom_df: pd.DataFrame, products_df: pd.DataFrame, pack_cost: float) -> float:
    b = bom_df[bom_df["세트ID"] == set_id].copy()
    if b.empty:
        return np.nan
    b["수량"] = pd.to_numeric(b["수량"], errors="coerce").fillna(0).astype(int)
    b = b.merge(products_df[["품번","원가","상품명"]], on="품번", how="left")
    b["원가"] = pd.to_numeric(b["원가"], errors="coerce").fillna(0.0)
    total = float((b["원가"] * b["수량"]).sum()) + float(pack_cost or 0.0)
    return total if total > 0 else np.nan

def classify_set(set_id: str, bom_df: pd.DataFrame, products_df: pd.DataFrame):
    b = bom_df[bom_df["세트ID"] == set_id].copy()
    if b.empty:
        return {"set_type":"assort", "non_acc_units":0, "hero_sku":None, "detail_df": pd.DataFrame()}

    b["수량"] = pd.to_numeric(b["수량"], errors="coerce").fillna(0).astype(int)
    b = b.merge(products_df[["품번","상품명","원가"]], on="품번", how="left")
    b["원가"] = pd.to_numeric(b["원가"], errors="coerce").fillna(0.0)

    b["is_acc"] = b.apply(lambda r: is_accessory_sku(r["품번"], r.get("상품명",""), r.get("원가",0)), axis=1)
    non_acc = b[~b["is_acc"]].copy()
    non_acc_units = int(non_acc["수량"].sum())
    unique_non_acc = int(non_acc["품번"].nunique())
    is_gift = bool(b["is_acc"].any())

    base_type = "multi" if (unique_non_acc <= 1 and non_acc_units > 0) else "assort"
    set_type = "gift" if is_gift else base_type

    hero_sku = None
    if not non_acc.empty:
        hero_sku = str(non_acc.sort_values("원가", ascending=False).iloc[0]["품번"])

    return {"set_type": set_type, "non_acc_units": non_acc_units, "hero_sku": hero_sku, "detail_df": b}

def get_set_disc_pct(set_type: str, zone: str, pack_units: int, disc_df: pd.DataFrame, params: dict) -> float:
    """Disc 테이블 + pack_units 보정(로그2)"""
    base = disc_df[(disc_df["세트타입"]==set_type) & (disc_df["가격영역"]==zone)]
    base_pct = float(base.iloc[0]["할인율(%)"]) if not base.empty else 0.0

    step = float(params.get("disc_pack_step_pct", 2.0))
    cap = float(params.get("disc_pack_cap_pct", 6.0))
    add = 0.0 if pack_units <= 1 else min(cap, step*np.log2(pack_units))
    return float(np.clip(base_pct + add, 0.0, 95.0))

def estimate_sku_msrp(
    sku_cost: float,
    channels_df,
    zone_map,
    boundaries,
    rounding_unit,
    min_cm,
    min_cost_ratio_cap,
    always_cost_ratio_target,
    always_list_disc,
    policy,
    market_row,
):
    if sku_cost != sku_cost or sku_cost <= 0:
        return np.nan
    _, max_auto, _ = compute_auto_range(
        cost_total=float(sku_cost),
        channels_df=channels_df,
        zone_map=zone_map,
        boundaries=boundaries,
        rounding_unit=rounding_unit,
        min_cm=min_cm,
        min_cost_ratio_cap=min_cost_ratio_cap,
        always_cost_ratio_target=always_cost_ratio_target,
        always_list_disc=always_list_disc,
        policy=policy,
        market_row=market_row,
        include_zones=PRICE_ZONES,
        min_zone="공구",
        msrp_override=np.nan,
        min_override=np.nan,
        max_override=np.nan,
    )
    return float(max_auto) if max_auto == max_auto else np.nan

def compute_predicted_sku_always(
    products_df: pd.DataFrame,
    channels_df: pd.DataFrame,
    zone_map: dict,
    boundaries: list,
    rounding_unit: int,
    min_cm: float,
    min_cost_ratio_cap: float,
    always_cost_ratio_target: float,
    always_list_disc: float,
    overrides_df: pd.DataFrame,
    policy: str,
    market_df: pd.DataFrame,
):
    """
    세트 BASE 산출용: SKU의 '상시' 가격(최종)을 예측.
    - 정책/시장앵커 반영 가능 (policy, market_df)
    - 오버라이드는 overrides_df에서 반영
    """
    sku_always = {}
    if products_df is None or products_df.empty:
        return sku_always

    default_tp = default_zone_target_pos(boundaries)

    market_map = {}
    if market_df is not None and not market_df.empty and "품번" in market_df.columns:
        market_map = market_df.set_index("품번").to_dict("index")

    for _, rr in products_df.iterrows():
        sku = str(rr.get("품번", "")).strip()
        if not sku:
            continue
        cost = safe_float(rr.get("원가", np.nan), np.nan)
        if cost != cost or cost <= 0:
            continue

        mrow = market_map.get(sku, None)
        min_auto, max_auto, _ = compute_auto_range(
            cost_total=cost,
            channels_df=channels_df,
            zone_map=zone_map,
            boundaries=boundaries,
            rounding_unit=rounding_unit,
            min_cm=min_cm,
            min_cost_ratio_cap=min_cost_ratio_cap,
            always_cost_ratio_target=always_cost_ratio_target,
            always_list_disc=always_list_disc,
            policy=policy,
            market_row=mrow,
            include_zones=PRICE_ZONES,
            min_zone="공구",
            msrp_override=safe_float(rr.get("MSRP_오버라이드", np.nan), np.nan),
            min_override=safe_float(rr.get("Min_오버라이드", np.nan), np.nan),
            max_override=safe_float(rr.get("Max_오버라이드", np.nan), np.nan),
        )

        zdf = build_zone_table(
            cost_total=cost,
            min_price=float(min_auto),
            max_price=float(max_auto),
            channels_df=channels_df,
            zone_map=zone_map,
            boundaries=boundaries,
            target_pos=default_tp,
            rounding_unit=rounding_unit,
            min_cm=min_cm,
            overrides_df=overrides_df,
            item_type="SKU",
            item_id=sku,
        )
        if zdf is None or zdf.empty or "가격영역" not in zdf.columns:
            continue
        ar = zdf[zdf["가격영역"] == "상시"]
        if ar.empty:
            continue
        p = safe_float(ar.iloc[0].get("최종가격(원)", np.nan), np.nan)
        if p == p and p > 0:
            sku_always[sku] = float(p)

    return sku_always

def compute_set_anchors(
    set_id: str,
    bom_df: pd.DataFrame,
    products_df: pd.DataFrame,
    sku_always: dict,
    params: dict,
    policy: str,
    market_df: pd.DataFrame,
    channels_df,
    zone_map,
    boundaries,
    rounding_unit,
    min_cm,
    min_cost_ratio_cap,
    always_cost_ratio_target,
    always_list_disc
):
    cls = classify_set(set_id, bom_df, products_df)
    b = cls.get("detail_df", pd.DataFrame()).copy()
    if b.empty:
        return None

    set_type = cls["set_type"]
    pack_cost = float(params.get("pack_cost_gift", 700.0)) if set_type=="gift" else float(params.get("pack_cost_default", 0.0))

    b["is_acc"] = b.apply(lambda r: is_accessory_sku(r["품번"], r.get("상품명",""), r.get("원가",0)), axis=1)
    b["상시_ref"] = b["품번"].astype(str).map(sku_always).astype(float).fillna(0.0)
    b.loc[b["is_acc"], "상시_ref"] = 0.0
    base_sum = float((b["상시_ref"] * b["수량"]).sum())

    # market-aware msrp sum estimate (exclude accessories)
    market_map = {}
    if market_df is not None and not market_df.empty and "품번" in market_df.columns:
        market_map = market_df.set_index("품번").to_dict("index")

    msrp_sum = 0.0
    for _, rr in b.iterrows():
        if rr["is_acc"]:
            continue
        sku = str(rr["품번"])
        sku_msrp = estimate_sku_msrp(
            sku_cost=safe_float(rr.get("원가", np.nan), np.nan),
            channels_df=channels_df,
            zone_map=zone_map,
            boundaries=boundaries,
            rounding_unit=rounding_unit,
            min_cm=min_cm,
            min_cost_ratio_cap=min_cost_ratio_cap,
            always_cost_ratio_target=always_cost_ratio_target,
            always_list_disc=always_list_disc,
            policy=policy,
            market_row=market_map.get(sku, None),
        )
        if sku_msrp == sku_msrp:
            msrp_sum += float(sku_msrp) * int(rr["수량"])

    k = float(params.get("k_msrp_set_multi", 1.00)) if set_type=="multi" else (
        float(params.get("k_msrp_set_assort", 0.98)) if set_type=="assort" else float(params.get("k_msrp_set_gift", 1.03))
    )
    msrp_set_sum = msrp_sum * k
    pack_units = int(cls.get("non_acc_units", 0)) if cls.get("non_acc_units",0) > 0 else 1

    # fallback
    if base_sum <= 0 and msrp_sum > 0:
        base_sum = msrp_sum * 0.85

    return {
        "set_type": set_type,
        "pack_cost": pack_cost,
        "pack_units": pack_units,
        "base_sum": base_sum,
        "msrp_sum_est": msrp_sum,
        "msrp_set_sum": msrp_set_sum,
        "hero_sku": cls.get("hero_sku"),
        "detail_df": b
    }

def compute_set_range(
    cost_total: float,
    anchors: dict,
    channels_df,
    zone_map,
    boundaries,
    rounding_unit,
    min_cm,
    min_cost_ratio_cap,
    always_cost_ratio_target,
    always_list_disc,
    policy: str,
    msrp_override=np.nan,
    min_override=np.nan,
    max_override=np.nan,
):
    # For sets, Min is still cost-based (floor + ratio).
    min_auto, max_cost, meta = compute_auto_range(
        cost_total=cost_total,
        channels_df=channels_df,
        zone_map=zone_map,
        boundaries=boundaries,
        rounding_unit=rounding_unit,
        min_cm=min_cm,
        min_cost_ratio_cap=min_cost_ratio_cap,
        always_cost_ratio_target=always_cost_ratio_target,
        always_list_disc=always_list_disc,
        policy="cost_only",  # set Min/Max baseline from cost policy (stable)
        market_row=None,
        include_zones=PRICE_ZONES,
        min_zone="공구",
        msrp_override=np.nan,
        min_override=min_override,
        max_override=np.nan
    )

    candidates = [max_cost] if max_cost==max_cost else []
    if anchors and anchors.get("msrp_set_sum", np.nan) == anchors.get("msrp_set_sum", np.nan):
        candidates.append(float(anchors["msrp_set_sum"]))
    if msrp_override == msrp_override and msrp_override > 0:
        candidates.append(float(msrp_override))
    if max_override == max_override and max_override > 0:
        candidates.append(float(max_override))

    max_auto = krw_ceil(max(candidates), rounding_unit) if candidates else max_cost
    if max_auto <= min_auto:
        max_auto = krw_ceil(min_auto + max(rounding_unit*10, int(min_auto*0.15)), rounding_unit)

    meta2 = dict(meta)
    meta2["note"] = f"[SET] Min/Max from cost baseline + anchor(msrp_set_sum) / (selected policy={policy})"
    return float(min_auto), float(max_auto), meta2

def build_zone_table_set(
    cost_total: float,
    min_price: float,
    max_price: float,
    anchors: dict,
    channels_df,
    zone_map,
    boundaries,
    rounding_unit,
    min_cm,
    overrides_df,
    disc_df,
    params,
    item_id: str
):
    ch_map = channels_df.set_index("채널명").to_dict("index")
    if min_price != min_price or max_price != max_price or max_price <= min_price or anchors is None:
        return pd.DataFrame(columns=[
            '가격영역', '세트타입', '팩수량(부자재제외)', 'Disc(%)', '비용채널', 'BandLow', 'BandHigh',
            'Floor(손익하한)', '추천가(Target)', '가격_오버라이드(원)', '최종가격(원)', '상태', '경고',
            '마진룸(원)=최종-Floor', '기여이익(원)', '기여이익률(%)'
        ])

    rows = []
    span = max_price - min_price
    set_type = anchors["set_type"]
    base_sum = float(anchors.get("base_sum", 0.0))
    pack_units = int(anchors.get("pack_units", 1))

    for i, z in enumerate(PRICE_ZONES):
        start = boundaries[i] / 100.0
        end = boundaries[i+1] / 100.0
        band_low = min_price + span * start
        band_high = min_price + span * end

        ch = zone_map.get(z, "자사몰")
        p = ch_map.get(ch, None)
        if p is None:
            continue
        floor = floor_price(cost_total, 1, p["수수료율"], p["PG"], p["마케팅비"], p["배송비(주문당)"], p["반품률"], p["반품비(주문당)"], min_cm)

        status = "OK"
        if z == "MSRP":
            target = max_price
            disc_pct = 0.0
        else:
            disc_pct = get_set_disc_pct(set_type, z, pack_units, disc_df, params)
            target_raw = base_sum * (1.0 - disc_pct/100.0)
            target = max(target_raw, floor)

            if floor > band_high:
                status = "불가(Floor>BandHigh)"
                target = band_high
            else:
                if target > band_high:
                    status = "조정(Target→BandHigh)"
                    target = band_high
                if target < band_low:
                    status = "조정(Target→BandLow)"
                    target = band_low

        ov = overrides_df[(overrides_df["오퍼타입"]=="SET") & (overrides_df["오퍼ID"]==item_id) & (overrides_df["가격영역"]==z)]
        override_price = safe_float(ov.iloc[0]["가격_오버라이드"], np.nan) if not ov.empty else np.nan
        effective = override_price if (override_price==override_price and override_price>0) else target

        band_low_r = krw_round(band_low, rounding_unit)
        band_high_r = krw_round(band_high, rounding_unit)
        floor_r = krw_round(floor, rounding_unit)
        target_r = krw_round(target, rounding_unit)
        eff_r = krw_round(effective, rounding_unit) if (effective==effective and effective>0) else np.nan

        cm, cmr = contrib_metrics(eff_r if eff_r==eff_r else 0, cost_total, 1, p["수수료율"], p["PG"], p["마케팅비"], p["배송비(주문당)"], p["반품률"], p["반품비(주문당)"])

        flags = []
        if eff_r == eff_r and eff_r < floor_r: flags.append("⚠️ 손익하한 미만")
        if eff_r == eff_r and eff_r < band_low_r: flags.append("⚠️ 밴드하한 미만")
        if eff_r == eff_r and eff_r > band_high_r and z != "MSRP": flags.append("⚠️ 밴드상한 초과")

        rows.append({
            "가격영역": z,
            "세트타입": set_type,
            "팩수량(부자재제외)": pack_units,
            "Disc(%)": round(float(disc_pct),1),
            "비용채널": ch,
            "BandLow": band_low_r,
            "BandHigh": band_high_r,
            "Floor(손익하한)": floor_r,
            "추천가(Target)": target_r,
            "가격_오버라이드(원)": (krw_round(override_price, rounding_unit) if override_price==override_price else np.nan),
            "최종가격(원)": eff_r,
            "상태": status,
            "경고": " / ".join(flags),
            "마진룸(원)=최종-Floor": (eff_r - floor_r) if eff_r==eff_r else np.nan,
            "기여이익(원)": int(round(cm)) if cm==cm else np.nan,
            "기여이익률(%)": round(cmr*100,1) if cmr==cmr else np.nan,
        })

    return pd.DataFrame(rows)

# -----------------------------
# History parsing & calibration (kept)
# -----------------------------
def load_history_table(file) -> pd.DataFrame:
    if file is None:
        return pd.DataFrame()
    name = getattr(file, "name", "")
    if name.lower().endswith(".csv"):
        df = pd.read_csv(file)
    else:
        df = pd.read_excel(file)
    df.columns = [str(c).strip() for c in df.columns]
    return df

def parse_history_to_tables(df_raw: pd.DataFrame):
    """
    운영 가격표를 다음 규칙으로 파싱:
    - 세트 행: 'No'가 숫자(=존재)이고, 품번이 비어있거나, 이름에 '세트'가 포함된 행
    - 구성품 행: 'No'가 비어있고, 품번이 존재하는 행 (세트 행 직전까지 누적이 BOM)
    """
    if df_raw.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    df = df_raw.copy()
    df.columns = [str(c).strip() for c in df.columns]

    # choose product name column (handles duplicated "신규품명")
    name_col = None
    for cand in ["신규품명.1", "신규품명_1", "신규품명 (2)", "신규품명"]:
        if cand in df.columns:
            name_col = cand
            break
    if name_col is None:
        for c in df.columns:
            if "신규품명" in c:
                name_col = c
                break
    if name_col is None:
        name_col = "상품명" if "상품명" in df.columns else None

    def parse_no(x):
        v = safe_float(x, np.nan)
        if v != v:
            return np.nan
        return int(v)

    df["_no"] = df["No"].apply(parse_no) if "No" in df.columns else np.nan
    df["_sku"] = df["품번"].astype(str).str.strip() if "품번" in df.columns else ""
    df["_name"] = df[name_col].astype(str).str.strip() if (name_col and name_col in df.columns) else ""

    money_cols = ["원가","폐쇄몰","공구가","홈쇼핑","모바일방송가","원데이특가","브랜드위크가","오프라인","상시할인가","소비자가"]
    for c in money_cols:
        if c in df.columns:
            df[c] = df[c].apply(safe_float)

    def is_set_row(r):
        no = r["_no"]
        sku = str(r["_sku"]).strip()
        nm = str(r["_name"])
        if no != no:
            return False
        if ("세트" in nm) or ("_세트_" in nm):
            return True
        if sku == "" or sku.lower() in ["nan", "none"]:
            return True
        return False

    def is_component_row(r):
        sku = str(r["_sku"]).strip()
        no = r["_no"]
        if sku == "" or sku.lower() in ["nan", "none"]:
            return False
        return not (no == no)

    components = []
    sets = []
    boms = []
    block_idx = 0

    for _, r in df.iterrows():
        if is_component_row(r):
            components.append({
                "품번": r["_sku"], "상품명": r["_name"],
                "원가": safe_float(r.get("원가", np.nan), np.nan),
                "소비자가": safe_float(r.get("소비자가", np.nan), np.nan),
                "폐쇄몰": safe_float(r.get("폐쇄몰", np.nan), np.nan),
                "공구가": safe_float(r.get("공구가", np.nan), np.nan),
                "홈쇼핑": safe_float(r.get("홈쇼핑", np.nan), np.nan),
                "모바일방송가": safe_float(r.get("모바일방송가", np.nan), np.nan),
                "원데이특가": safe_float(r.get("원데이특가", np.nan), np.nan),
                "브랜드위크가": safe_float(r.get("브랜드위크가", np.nan), np.nan),
                "오프라인": safe_float(r.get("오프라인", np.nan), np.nan),
                "상시할인가": safe_float(r.get("상시할인가", np.nan), np.nan),
            })
            continue

        if is_set_row(r):
            block_idx += 1
            set_id = f"S{block_idx:04d}"
            sets.append({
                "set_id": set_id,
                "source_No": r["_no"],
                "set_name": r["_name"],
                "원가": safe_float(r.get("원가", np.nan), np.nan),
                "소비자가": safe_float(r.get("소비자가", np.nan), np.nan),
                "폐쇄몰": safe_float(r.get("폐쇄몰", np.nan), np.nan),
                "공구가": safe_float(r.get("공구가", np.nan), np.nan),
                "홈쇼핑": safe_float(r.get("홈쇼핑", np.nan), np.nan),
                "모바일방송가": safe_float(r.get("모바일방송가", np.nan), np.nan),
                "원데이특가": safe_float(r.get("원데이특가", np.nan), np.nan),
                "브랜드위크가": safe_float(r.get("브랜드위크가", np.nan), np.nan),
                "오프라인": safe_float(r.get("오프라인", np.nan), np.nan),
                "상시할인가": safe_float(r.get("상시할인가", np.nan), np.nan),
            })
            if components:
                comp_df = pd.DataFrame(components)
                comp_df["품번"] = comp_df["품번"].astype(str).str.strip()
                g = comp_df.groupby("품번", as_index=False).agg(qty=("품번","size"), 상품명=("상품명","first"), 원가=("원가","median"))
                for _, cr in g.iterrows():
                    boms.append({"set_id": set_id, "품번": cr["품번"], "수량": int(cr["qty"]), "상품명": cr["상품명"], "원가": cr["원가"]})
            components = []
            continue

    return pd.DataFrame(components), pd.DataFrame(sets), pd.DataFrame(boms)

def zone_from_history_column(col: str) -> str:
    mapping = {
        "폐쇄몰": "폐쇄몰",
        "공구가": "공구",
        "홈쇼핑": "홈쇼핑",
        "모바일방송가": "모바일라방",
        "원데이특가": "원데이",
        "브랜드위크가": "브랜드위크",
        "오프라인": "오프라인",
        "상시할인가": "상시",
        "소비자가": "MSRP",
    }
    return mapping.get(col, "")


def get_max_channel_fee(channels_df):
    """채널 비용 데이터 중 가장 높은 수수료율을 반환. 역산에서 사용."""
    if channels_df is None or channels_df.empty:
        return 0.50
    fees = pd.to_numeric(channels_df["수수료율"], errors="coerce")
    return float(fees.max()) if not fees.empty else 0.50

def calc_reverse_fob(base_price, target_rate, exchange_rate, tariff_rate, freight_mult=1.2):
    """역산 FOB 계산. 반환: dict(landing_cost, fob, fob_krw)"""
    if base_price <= 0 or exchange_rate <= 0:
        return {"landing_cost": np.nan, "fob": np.nan, "fob_krw": np.nan}
    landing = base_price * target_rate
    fob_krw = landing / freight_mult / (1.0 + tariff_rate)
    fob = fob_krw / exchange_rate
    return {"landing_cost": landing, "fob": fob, "fob_krw": fob_krw}

def determine_rrp(rrp_method, consumer_price, overseas_price, exchange_rate):
    """RRP 산정방식에 따라 기준판매가 결정"""
    if rrp_method == "국내공식가":
        return krw_round(safe_float(consumer_price, 0) * 0.9, 100)
    elif rrp_method == "해외공식가×1.1":
        op = safe_float(overseas_price, 0)
        er = safe_float(exchange_rate, 1400)
        if op > 0 and er > 0:
            return int(np.ceil(op * er * 1.1 / 1000) * 1000)
        return 0
    elif rrp_method == "경쟁사조사역산":
        return int(safe_float(consumer_price, 0))
    else:
        return int(safe_float(consumer_price, 0))

def judge_cost_ratio(actual_cost, base_price):
    """현재 원가율이 어느 구간인지 판정"""
    if base_price <= 0 or actual_cost <= 0:
        return "데이터없음"
    ratio = actual_cost / base_price
    if ratio <= 0.25:
        return "최선(≤25%)"
    elif ratio <= 0.30:
        return "진행가능(≤30%)"
    elif ratio <= 0.35:
        return "논의필요(≤35%)"
    else:
        return "드랍(>35%)"

def process_bulk_skus(bulk_df, channels_df, zone_map, boundaries, target_pos, overrides_df,
                      rounding_unit, min_cm, min_cost_ratio_cap, always_cost_ratio_target,
                      always_list_disc, policy, market_df):
    """SKU 일괄 처리. bulk_df에서 순방향+역산을 한번에 돌림."""
    forward_rows = []
    reverse_rows = []
    warn_rows = []
    
    market_map = {}
    if market_df is not None and not market_df.empty and "품번" in market_df.columns:
        market_map = market_df.set_index("품번").to_dict("index")
    
    for _, row in bulk_df.iterrows():
        sku = str(row.get("품번", "")).strip()
        name = str(row.get("상품명", "")).strip()
        cost = safe_float(row.get("원가(VAT-)", np.nan), np.nan)
        consumer_price = safe_float(row.get("소비자가(VAT-)", np.nan), np.nan)
        cat = str(row.get("카테고리", "")).strip()
        rrp_method = str(row.get("RRP산정방식", "직접입력")).strip()
        overseas_price = safe_float(row.get("해외공식가", np.nan), np.nan)
        currency_code = str(row.get("통화", "USD")).strip()
        exchange_rate = safe_float(row.get("환율", np.nan), np.nan)
        tariff = safe_float(row.get("관세율(%)", np.nan), np.nan)
        market_type = str(row.get("시장유형", "미입력")).strip()
        same_price = safe_float(row.get("동일상품_시장가", np.nan), np.nan)
        comp_anchor = safe_float(row.get("경쟁카테고리_앵커가", np.nan), np.nan)
        
        if not sku:
            continue
        
        cur_data = next((c for c in CURRENCIES if c["code"] == currency_code), CURRENCIES[0])
        if exchange_rate != exchange_rate or exchange_rate <= 0:
            exchange_rate = cur_data["default_rate"]
        if tariff != tariff or tariff < 0:
            tariff = 0
        
        # --- 순방향 ---
        has_cost = (cost == cost and cost > 0)
        if has_cost:
            mrow = market_map.get(sku, None)
            if mrow is None and (market_type != "미입력" or (same_price == same_price and same_price > 0) or (comp_anchor == comp_anchor and comp_anchor > 0)):
                mrow = {
                    "시장유형": market_type,
                    "동일상품_시장가": same_price if same_price == same_price else np.nan,
                    "경쟁카테고리_앵커가": comp_anchor if comp_anchor == comp_anchor else np.nan,
                    "경쟁앵커_배수": 1.0,
                    "동일상품_가중치": 0.7,
                    "MSRP_시장오버라이드": np.nan,
                }
            
            min_auto, max_auto, meta = compute_auto_range(
                cost_total=cost,
                channels_df=channels_df,
                zone_map=zone_map,
                boundaries=boundaries,
                rounding_unit=rounding_unit,
                min_cm=min_cm,
                min_cost_ratio_cap=min_cost_ratio_cap,
                always_cost_ratio_target=always_cost_ratio_target,
                always_list_disc=always_list_disc,
                policy=policy,
                market_row=mrow,
                include_zones=PRICE_ZONES,
                min_zone="공구",
                msrp_override=np.nan,
                min_override=np.nan,
                max_override=np.nan,
            )
            
            if min_auto == min_auto and max_auto == max_auto and max_auto > min_auto:
                zdf = build_zone_table(
                    cost_total=cost,
                    min_price=float(min_auto),
                    max_price=float(max_auto),
                    channels_df=channels_df,
                    zone_map=zone_map,
                    boundaries=boundaries,
                    target_pos=target_pos,
                    rounding_unit=rounding_unit,
                    min_cm=min_cm,
                    overrides_df=overrides_df,
                    item_type="SKU",
                    item_id=sku,
                )
                
                fwd = {"품번": sku, "상품명": name, "원가": int(cost), "정책": policy,
                       "Min": int(min_auto), "MSRP": int(max_auto)}
                if not zdf.empty and "가격영역" in zdf.columns and "최종가격(원)" in zdf.columns:
                    for _, zrow in zdf.iterrows():
                        zone_name = zrow["가격영역"]
                        zone_price = zrow.get("최종가격(원)", np.nan)
                        if zone_price == zone_price:
                            fwd[zone_name] = int(zone_price)
                forward_rows.append(fwd)
            else:
                warn_rows.append({"품번": sku, "이슈": "순방향 산출 실패", "상세": str(meta.get("note", ""))})
        
        # --- 역산 ---
        has_consumer = (consumer_price == consumer_price and consumer_price > 0)
        has_overseas = (overseas_price == overseas_price and overseas_price > 0)
        
        base_price = 0
        base_note = ""
        
        if has_consumer:
            base_price = determine_rrp(rrp_method, consumer_price, overseas_price, exchange_rate)
            base_note = rrp_method
        elif has_overseas and exchange_rate > 0:
            base_price = int(np.ceil(overseas_price * exchange_rate * 1.1 / 1000) * 1000)
            base_note = "해외공식가×1.1 자동산정"
            warn_rows.append({"품번": sku, "이슈": "소비자가 미입력",
                             "상세": f"해외공식가 {cur_data['symbol']}{overseas_price} → RRP {base_price:,}원 자동산정"})
        
        if base_price > 0:
            tariff_dec = tariff / 100.0
            rev = {"품번": sku, "상품명": name, "카테고리": cat, "기준판매가": int(base_price),
                   "통화": currency_code, "환율": exchange_rate, "관세율": f"{tariff}%"}
            
            for tier in REVERSE_TIERS:
                r = calc_reverse_fob(base_price, tier["rate"], exchange_rate, tariff_dec)
                pct_label = f"FOB({int(tier['rate']*100)}%)"
                rev[pct_label] = round(r["fob"], 2) if r["fob"] == r["fob"] else np.nan
            
            if has_overseas and overseas_price > 0:
                r25 = calc_reverse_fob(base_price, 0.25, exchange_rate, tariff_dec)
                if r25["fob_krw"] == r25["fob_krw"]:
                    rev["해외가대비(25%)"] = f"{r25['fob_krw'] / (overseas_price * exchange_rate) * 100:.1f}%"
            
            if has_cost:
                rev["판정"] = judge_cost_ratio(cost, base_price)
            else:
                rev["판정"] = ""
            
            reverse_rows.append(rev)
    
    forward_df = pd.DataFrame(forward_rows) if forward_rows else pd.DataFrame()
    reverse_df = pd.DataFrame(reverse_rows) if reverse_rows else pd.DataFrame()
    warnings_df = pd.DataFrame(warn_rows) if warn_rows else pd.DataFrame()
    
    return forward_df, reverse_df, warnings_df

def calibrate_set_disc_from_history(set_df, bom_df_hist, products_df, sku_always_pred, params, disc_df):
    if set_df.empty or bom_df_hist.empty:
        return disc_df, pd.DataFrame()

    bom_app = bom_df_hist.rename(columns={"set_id":"세트ID"})[["세트ID","품번","수량"]].copy()
    obs_rows = []

    for _, sr in set_df.iterrows():
        sid = sr["set_id"]
        cls = classify_set(sid, bom_app, products_df[["품번","상품명","원가"]].copy())
        set_type = cls.get("set_type","assort")
        pack_units = int(cls.get("non_acc_units",0)) if cls.get("non_acc_units",0)>0 else 1
        detail = cls.get("detail_df", pd.DataFrame()).copy()
        if detail.empty:
            continue
        detail["is_acc"] = detail.apply(lambda r: is_accessory_sku(r["품번"], r.get("상품명",""), r.get("원가",0)), axis=1)
        detail["상시_pred"] = detail["품번"].astype(str).map(sku_always_pred).astype(float).fillna(0.0)
        detail.loc[detail["is_acc"], "상시_pred"] = 0.0
        base_sum = float((detail["상시_pred"] * detail["수량"]).sum())
        if base_sum <= 0:
            continue

        for col in ["폐쇄몰","공구가","홈쇼핑","모바일방송가","원데이특가","브랜드위크가","오프라인","상시할인가"]:
            p = safe_float(sr.get(col, np.nan), np.nan)
            if p != p or p <= 0:
                continue
            zone = zone_from_history_column(col)
            disc_obs = 1.0 - (p / base_sum)
            step = float(params.get("disc_pack_step_pct", 2.0))
            cap = float(params.get("disc_pack_cap_pct", 6.0))
            add = 0.0 if pack_units<=1 else min(cap, step*np.log2(pack_units))
            base_disc = disc_obs*100.0 - add
            obs_rows.append({
                "set_id":sid,"set_type":set_type,"pack_units":pack_units,"zone":zone,
                "price_actual":p,"base_sum_pred":base_sum,
                "disc_obs_pct":disc_obs*100.0,"add_pct":add,"base_disc_pct":base_disc
            })

    obs = pd.DataFrame(obs_rows)
    if obs.empty:
        return disc_df, obs

    new_disc = disc_df.copy()
    for stype in SET_TYPES:
        for z in PRICE_ZONES:
            if z == "MSRP":
                continue
            sub = obs[(obs["set_type"]==stype) & (obs["zone"]==z)]
            if sub.empty:
                continue
            med = float(np.nanmedian(sub["base_disc_pct"].values))
            new_disc.loc[(new_disc["세트타입"]==stype) & (new_disc["가격영역"]==z), "할인율(%)"] = round(max(0.0, min(95.0, med)), 1)

    return new_disc, obs

# -----------------------------
# Session state
# -----------------------------

    if "bulk_forward" not in st.session_state:
        st.session_state["bulk_forward"] = pd.DataFrame()
    if "bulk_reverse" not in st.session_state:
        st.session_state["bulk_reverse"] = pd.DataFrame()
    if "bulk_warnings" not in st.session_state:
        st.session_state["bulk_warnings"] = pd.DataFrame()
def init_state():
    if "products_df" not in st.session_state:
        st.session_state["products_df"] = pd.DataFrame(columns=["품번","상품명","브랜드","원가","MSRP_오버라이드","Min_오버라이드","Max_오버라이드","운영여부"])
    if "channels_df" not in st.session_state:
        st.session_state["channels_df"] = pd.DataFrame(DEFAULT_CHANNELS, columns=["채널명","수수료율","PG","배송비(주문당)","마케팅비","반품률","반품비(주문당)"])
    if "zone_map" not in st.session_state:
        st.session_state["zone_map"] = DEFAULT_ZONE_MAP.copy()
    if "boundaries" not in st.session_state:
        st.session_state["boundaries"] = DEFAULT_BOUNDARIES.copy()
    if "target_pos" not in st.session_state:
        st.session_state["target_pos"] = default_zone_target_pos(st.session_state["boundaries"])
    if "overrides_df" not in st.session_state:
        st.session_state["overrides_df"] = pd.DataFrame(columns=["오퍼타입","오퍼ID","가격영역","가격_오버라이드"])
    if "set_disc_df" not in st.session_state:
        st.session_state["set_disc_df"] = make_default_set_disc_df()
    if "set_params" not in st.session_state:
        st.session_state["set_params"] = DEFAULT_SET_PARAMS.copy()
    if "history_set_df" not in st.session_state:
        st.session_state["history_set_df"] = pd.DataFrame()
    if "history_bom_df" not in st.session_state:
        st.session_state["history_bom_df"] = pd.DataFrame()
    if "sets_df" not in st.session_state:
        st.session_state["sets_df"] = pd.DataFrame(columns=["세트ID","세트명","MSRP_오버라이드"])
    if "bom_df" not in st.session_state:
        st.session_state["bom_df"] = pd.DataFrame(columns=["세트ID","품번","수량"])
    if "market_df" not in st.session_state:
        st.session_state["market_df"] = pd.DataFrame(columns=[
            "품번","시장유형","동일상품_시장가","경쟁카테고리_앵커가","경쟁앵커_배수","동일상품_가중치","MSRP_시장오버라이드","메모"
        ])
    if "validation_export" not in st.session_state:
        st.session_state["validation_export"] = None

init_state()

# -----------------------------
# UI
# -----------------------------
st.title("IBR 가격 시뮬레이터 v7")
st.caption("원가 기반 엔진 + 시장가 앵커 + 다중 정책 비교 + 역산 FOB + SKU 일괄 처리")

tab_up, tab_market, tab_sku, tab_set, tab_bulk, tab_reverse, tab_cal = st.tabs([
    "1) 업로드/설정", "2) 시장가 앵커", "3) 단품(다중정책)",
    "4) 세트(BOM)", "5) SKU 일괄처리", "6) 역산(FOB)", "7) 캘리브레이션/검증"
])


with tab_up:
    st.subheader("A. 원가/상품마스터 업로드(필수)")
    st.download_button(
        "원가 업로드 템플릿 다운로드(.xlsx)",
        data=make_cost_master_template_bytes(),
        file_name="원가_상품마스터_업로드양식.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        help="상품코드/상품명/브랜드/원가(vat-)만 채워서 업로드하면 됩니다."
    )

    up = st.file_uploader("원가/상품마스터 업로드(.xlsx)", type=["xlsx","xls"])
    if up is not None:
        try:
            st.session_state["products_df"] = load_products_from_cost_master(up)
            st.success(f"업로드 완료: {len(st.session_state['products_df']):,}개 SKU")
        except Exception as e:
            st.error(f"업로드 오류: {e}")

    st.metric("현재 SKU 수", f"{len(st.session_state['products_df']):,}")

    st.divider()
    st.subheader("B. 채널 비용(키인 즉시 반영)")
    st.session_state["channels_df"] = st.data_editor(
        st.session_state["channels_df"],
        use_container_width=True,
        num_rows="dynamic",
        height=260
    )

    st.divider()
    st.subheader("C. 가격영역(밴드) ↔ 비용채널 매핑")
    zone_map = st.session_state["zone_map"].copy()
    channel_names = st.session_state["channels_df"]["채널명"].dropna().astype(str).tolist()
    cols = st.columns(5)
    for i, z in enumerate(PRICE_ZONES):
        with cols[i % 5]:
            zone_map[z] = st.selectbox(
                f"{z}",
                options=channel_names,
                index=channel_names.index(zone_map.get(z, channel_names[0])) if zone_map.get(z) in channel_names else 0,
                key=f"zmap_{z}"
            )
    st.session_state["zone_map"] = zone_map

    st.divider()
    st.subheader("D. 밴드 경계(%)")
    b = st.session_state["boundaries"].copy()
    prev = 0
    new_b = [0]
    for idx in range(1, 10):
        minv = prev + 1
        maxv = 100 - (10-idx)
        val = int(b[idx])
        val = max(minv, min(maxv, val))
        val = st.slider(
            f"경계 {idx}: {PRICE_ZONES[idx-1]} | {PRICE_ZONES[idx]} (%)",
            min_value=minv, max_value=maxv, value=val, step=1, key=f"b_{idx}"
        )
        new_b.append(val); prev = val
    new_b.append(100)
    st.session_state["boundaries"] = new_b
    st.session_state["target_pos"] = default_zone_target_pos(new_b)

with tab_market:
    st.subheader("시장가 앵커 입력(선택이지만 권장)")
    st.caption("국내 신상품: 경쟁카테고리 앵커가 필수 / 해외소싱 동일상품: 동일상품 시장가 + 경쟁카테고리 앵커가 권장")

    prod = st.session_state["products_df"].copy()
    if prod.empty:
        st.warning("먼저 원가/상품마스터를 업로드하세요.")
    else:
        # seed rows for existing SKUs if empty
        if st.session_state["market_df"].empty:
            seed = prod[["품번"]].copy()
            seed["시장유형"] = "미입력"
            seed["동일상품_시장가"] = np.nan
            seed["경쟁카테고리_앵커가"] = np.nan
            seed["경쟁앵커_배수"] = 1.0
            seed["동일상품_가중치"] = 0.7
            seed["MSRP_시장오버라이드"] = np.nan
            seed["메모"] = ""
            st.session_state["market_df"] = seed

        st.session_state["market_df"] = st.data_editor(
            st.session_state["market_df"],
            use_container_width=True,
            num_rows="dynamic",
            height=420,
            column_config={
                "시장유형": st.column_config.SelectboxColumn("시장유형", options=MARKET_TYPES),
            },
        )
        st.info("💡 앵커는 MSRP/상시의 '출발점'입니다. 최저가(Min)는 항상 원가/채널손익 기반으로 안전장치를 둡니다.")

with tab_sku:
    st.subheader("단품: 다중 정책 결과 비교")
    prod = st.session_state["products_df"].copy()
    if prod.empty:
        st.warning("원가 업로드 후 사용하세요.")
    else:
        # global parameters for SKU
        c1,c2,c3,c4 = st.columns([1,1,1,1])
        with c1:
            rounding_unit = st.selectbox("반올림 단위", [10,100,1000], index=2, key="sku_round")
        with c2:
            min_cost_ratio_cap = st.number_input("최저가 원가율 상한", min_value=0.05, max_value=0.95, value=0.30, step=0.01, format="%.2f", key="sku_min_ratio")
        with c3:
            always_cost_ratio_target = st.number_input("상시 목표 원가율", min_value=0.05, max_value=0.95, value=0.18, step=0.01, format="%.2f", key="sku_always_ratio")
            always_list_disc = st.slider("상시할인율(MSRP 대비) %", 0, 80, 20, 1, key="sku_list_disc") / 100.0
        with c4:
            min_cm = st.slider("최소 기여이익률(%)", 0, 50, 15, 1, key="sku_cm") / 100.0

        options = (prod["품번"].astype(str) + " | " + prod["상품명"].astype(str)).tolist()
        picked = st.selectbox("SKU 선택", options, index=0, key="sku_pick")
        sku = picked.split(" | ",1)[0].strip()
        row = prod[prod["품번"].astype(str)==sku].iloc[0]
        cost = safe_float(row["원가"], np.nan)

        market_df = st.session_state["market_df"].copy()
        mrow = None
        if not market_df.empty and "품번" in market_df.columns:
            sub = market_df[market_df["품번"].astype(str)==sku]
            if not sub.empty:
                mrow = sub.iloc[0].to_dict()

        st.markdown(f"**SKU:** `{sku}` — {row.get('상품명','')}")
        st.write(f"- 원가: **{int(cost):,}원**" if cost==cost else "- 원가: (비어있음)")

        if cost != cost or cost <= 0:
            st.error("원가가 비어있거나 0 이하입니다.")
        else:
            # Compare policies
            cmp_rows = []
            for pol in POLICIES:
                min_auto, max_auto, meta = compute_auto_range(
                    cost_total=cost,
                    channels_df=st.session_state["channels_df"],
                    zone_map=st.session_state["zone_map"],
                    boundaries=st.session_state["boundaries"],
                    rounding_unit=rounding_unit,
                    min_cm=min_cm,
                    min_cost_ratio_cap=min_cost_ratio_cap,
                    always_cost_ratio_target=always_cost_ratio_target,
                    always_list_disc=always_list_disc,
                    policy=pol["policy"],
                    market_row=mrow,
                    include_zones=PRICE_ZONES,
                    min_zone="공구",
                    msrp_override=safe_float(row.get("MSRP_오버라이드", np.nan), np.nan),
                    min_override=safe_float(row.get("Min_오버라이드", np.nan), np.nan),
                    max_override=safe_float(row.get("Max_오버라이드", np.nan), np.nan),
                )
                cmp_rows.append({
                    "policy": pol["policy"],
                    "설명": pol["설명"],
                    "Min": int(min_auto) if min_auto==min_auto else np.nan,
                    "Max/MSRP": int(max_auto) if max_auto==max_auto else np.nan,
                    "msrp_cost": meta.get("msrp_cost", np.nan),
                    "msrp_market": meta.get("msrp_market", np.nan),
                    "note": meta.get("note",""),
                })
            cmp_df = pd.DataFrame(cmp_rows)
            st.markdown("### 정책별 Min/Max 비교")
            st.dataframe(cmp_df[["policy","설명","Min","Max/MSRP","msrp_cost","msrp_market"]], use_container_width=True, height=220)

            chosen_pol = st.selectbox("상세 확인할 정책", [p["policy"] for p in POLICIES], index=2, key="sku_policy_pick")

            # Detailed table
            min_auto, max_auto, meta = compute_auto_range(
                cost_total=cost,
                channels_df=st.session_state["channels_df"],
                zone_map=st.session_state["zone_map"],
                boundaries=st.session_state["boundaries"],
                rounding_unit=rounding_unit,
                min_cm=min_cm,
                min_cost_ratio_cap=min_cost_ratio_cap,
                always_cost_ratio_target=always_cost_ratio_target,
                always_list_disc=always_list_disc,
                policy=chosen_pol,
                market_row=mrow,
                include_zones=PRICE_ZONES,
                min_zone="공구",
                msrp_override=safe_float(row.get("MSRP_오버라이드", np.nan), np.nan),
                min_override=safe_float(row.get("Min_오버라이드", np.nan), np.nan),
                max_override=safe_float(row.get("Max_오버라이드", np.nan), np.nan),
            )
            if meta.get("note"):
                st.info(meta["note"])

            c1,c2 = st.columns(2)
            with c1:
                min_user = st.number_input("Min(최저가) 수정", min_value=0, value=int(min_auto), step=rounding_unit, key="sku_min_user")
            with c2:
                max_user = st.number_input("Max(최고가/MSRP) 수정", min_value=0, value=int(max_auto), step=rounding_unit, key="sku_max_user")

            if max_user <= min_user:
                st.warning("Max가 Min 이하입니다. Max를 올려주세요.")
                max_user = min_user + max(rounding_unit*10, int(min_user*0.15))

            zdf = build_zone_table(
                cost_total=cost,
                min_price=float(min_user),
                max_price=float(max_user),
                channels_df=st.session_state["channels_df"],
                zone_map=st.session_state["zone_map"],
                boundaries=st.session_state["boundaries"],
                target_pos=st.session_state["target_pos"],
                rounding_unit=rounding_unit,
                min_cm=min_cm,
                overrides_df=st.session_state["overrides_df"],
                item_type="SKU",
                item_id=sku,
            )
            st.dataframe(zdf, use_container_width=True, height=420)

with tab_set:
    st.subheader("세트(BOM): 구성 → 자동 추천가(Disc 반영)")
    prod = st.session_state["products_df"].copy()
    if prod.empty:
        st.warning("원가 파일을 먼저 업로드하세요.")
    else:
        c1,c2,c3 = st.columns([1,2,1])
        with c1:
            new_id = st.text_input("세트ID", value="", key="new_set_id")
        with c2:
            new_name = st.text_input("세트명", value="", key="new_set_name")
        with c3:
            if st.button("세트 추가", type="primary", disabled=(not new_id.strip() or not new_name.strip()), key="add_set"):
                sets = st.session_state["sets_df"].copy()
                if (sets["세트ID"] == new_id.strip()).any():
                    st.warning("이미 존재하는 세트ID")
                else:
                    sets = pd.concat([sets, pd.DataFrame([{"세트ID":new_id.strip(),"세트명":new_name.strip(),"MSRP_오버라이드":np.nan}])], ignore_index=True)
                    st.session_state["sets_df"] = sets
                    st.success("세트 추가 완료")

        if st.session_state["sets_df"].empty:
            st.info("세트를 먼저 추가하세요.")
        else:
            st.session_state["sets_df"] = st.data_editor(st.session_state["sets_df"], use_container_width=True, height=160, num_rows="dynamic", key="sets_editor")
            set_opts = (st.session_state["sets_df"]["세트ID"].astype(str) + " | " + st.session_state["sets_df"]["세트명"].astype(str)).tolist()
            picked = st.selectbox("편집할 세트 선택", set_opts, index=0, key="set_pick")
            set_id = picked.split(" | ",1)[0].strip()

            st.markdown("### BOM(구성품) 추가")
            sku_opts = (prod["품번"].astype(str) + " | " + prod["상품명"].astype(str)).tolist()
            a1,a2,a3 = st.columns([3,1,1])
            with a1:
                sku_pick = st.selectbox("구성품 SKU", sku_opts, index=0, key=f"bom_sku_{set_id}")
                sku = sku_pick.split(" | ",1)[0].strip()
            with a2:
                qty = st.number_input("수량", min_value=1, value=1, step=1, key=f"bom_qty_{set_id}")
            with a3:
                if st.button("추가", key=f"bom_add_{set_id}"):
                    bom = st.session_state["bom_df"].copy()
                    bom = pd.concat([bom, pd.DataFrame([{"세트ID":set_id,"품번":sku,"수량":int(qty)}])], ignore_index=True)
                    st.session_state["bom_df"] = bom
                    st.success("추가 완료")

            bom_view = st.session_state["bom_df"][st.session_state["bom_df"]["세트ID"]==set_id].copy()
            if bom_view.empty:
                st.info("BOM이 비어있습니다.")
            else:
                bom_view = bom_view.merge(prod[["품번","상품명","원가"]], on="품번", how="left")
                bom_view["is_acc(부자재)"] = bom_view.apply(lambda r: is_accessory_sku(r["품번"], r.get("상품명",""), r.get("원가",0)), axis=1)
                st.dataframe(bom_view, use_container_width=True, height=220)

            st.divider()
            st.markdown("### 세트 추천가")
            p1,p2,p3,p4 = st.columns([1,1,1,1])
            with p1:
                rounding_unit = st.selectbox("반올림 단위", [10,100,1000], index=2, key="set_round")
            with p2:
                min_cost_ratio_cap = st.number_input("최저가 원가율 상한", min_value=0.05, max_value=0.95, value=0.30, step=0.01, format="%.2f", key="set_min_ratio")
            with p3:
                always_cost_ratio_target = st.number_input("상시 목표 원가율", min_value=0.05, max_value=0.95, value=0.18, step=0.01, format="%.2f", key="set_always_ratio")
                always_list_disc = st.slider("상시할인율(MSRP 대비) %", 0, 80, 20, 1, key="set_list_disc") / 100.0
            with p4:
                min_cm = st.slider("최소 기여이익률(%)", 0, 50, 15, 1, key="set_cm") / 100.0

            chosen_pol = st.selectbox("세트용 SKU 앵커 정책(세트 MSRP 추정에 사용)", [p["policy"] for p in POLICIES], index=2, key="set_policy")

            if not bom_view.empty:
                sku_always = compute_predicted_sku_always(
                    products_df=st.session_state["products_df"],
                    channels_df=st.session_state["channels_df"],
                    zone_map=st.session_state["zone_map"],
                    boundaries=st.session_state["boundaries"],
                    rounding_unit=rounding_unit,
                    min_cm=min_cm,
                    min_cost_ratio_cap=min_cost_ratio_cap,
                    always_cost_ratio_target=always_cost_ratio_target,
                    always_list_disc=always_list_disc,
                    overrides_df=st.session_state["overrides_df"],
                    policy=chosen_pol,
                    market_df=st.session_state["market_df"],
                )

                anchors = compute_set_anchors(
                    set_id=set_id,
                    bom_df=st.session_state["bom_df"],
                    products_df=prod,
                    sku_always=sku_always,
                    params=st.session_state["set_params"],
                    policy=chosen_pol,
                    market_df=st.session_state["market_df"],
                    channels_df=st.session_state["channels_df"],
                    zone_map=st.session_state["zone_map"],
                    boundaries=st.session_state["boundaries"],
                    rounding_unit=rounding_unit,
                    min_cm=min_cm,
                    min_cost_ratio_cap=min_cost_ratio_cap,
                    always_cost_ratio_target=always_cost_ratio_target,
                    always_list_disc=always_list_disc,
                )
                if anchors is None:
                    st.error("세트 앵커 계산 실패")
                else:
                    cost_total = compute_set_cost(set_id, st.session_state["bom_df"], prod, anchors["pack_cost"])
                    min_auto, max_auto, meta = compute_set_range(
                        cost_total=cost_total,
                        anchors=anchors,
                        channels_df=st.session_state["channels_df"],
                        zone_map=st.session_state["zone_map"],
                        boundaries=st.session_state["boundaries"],
                        rounding_unit=rounding_unit,
                        min_cm=min_cm,
                        min_cost_ratio_cap=min_cost_ratio_cap,
                        always_cost_ratio_target=always_cost_ratio_target,
                        always_list_disc=always_list_disc,
                        policy=chosen_pol,
                    )
                    st.write(f"- 세트 원가합(+pack_cost): **{int(cost_total):,}원** | 자동 레인지: **{int(min_auto):,} ~ {int(max_auto):,}원**")
                    if meta.get("note"):
                        st.info(meta["note"])

                    c1,c2 = st.columns(2)
                    with c1:
                        min_user = st.number_input("Min 수정(세트)", min_value=0, value=int(min_auto), step=rounding_unit, key="set_min_user")
                    with c2:
                        max_user = st.number_input("Max 수정(세트)", min_value=0, value=int(max_auto), step=rounding_unit, key="set_max_user")

                    if max_user <= min_user:
                        max_user = min_user + max(rounding_unit*10, int(min_user*0.15))

                    zdf = build_zone_table_set(
                        cost_total=float(cost_total),
                        min_price=float(min_user),
                        max_price=float(max_user),
                        anchors=anchors,
                        channels_df=st.session_state["channels_df"],
                        zone_map=st.session_state["zone_map"],
                        boundaries=st.session_state["boundaries"],
                        rounding_unit=rounding_unit,
                        min_cm=min_cm,
                        overrides_df=st.session_state["overrides_df"],
                        disc_df=st.session_state["set_disc_df"],
                        params=st.session_state["set_params"],
                        item_id=set_id
                    )
                    st.dataframe(zdf, use_container_width=True, height=420)

with tab_cal:
    st.subheader("기존 운영 가격표 업로드 → 세트 Disc(할인율) 역산 및 검증")
    st.caption("세트는 '세트(No 있는 행) 직전 누적된 SKU행이 BOM' 규칙으로 파싱됩니다.")

    up_hist = st.file_uploader("운영 가격표 업로드(.xlsx/.csv)", type=["xlsx","xls","csv"], key="hist_up")
    if up_hist is not None:
        try:
            raw = load_history_table(up_hist)
            _, set_hist, bom_hist = parse_history_to_tables(raw)
            st.session_state["history_set_df"] = set_hist
            st.session_state["history_bom_df"] = bom_hist
            st.success(f"파싱 완료: 세트 {len(set_hist):,} / BOM라인 {len(bom_hist):,}")
        except Exception as e:
            st.error(f"파싱 오류: {e}")

    set_hist = st.session_state["history_set_df"]
    bom_hist = st.session_state["history_bom_df"]

    c1,c2,c3,c4 = st.columns([1,1,1,1])
    with c1:
        rounding_unit = st.selectbox("반올림 단위(캘)", [10,100,1000], index=2, key="cal_round")
    with c2:
        min_cost_ratio_cap = st.number_input("최저가 원가율 상한(캘)", min_value=0.05, max_value=0.95, value=0.30, step=0.01, format="%.2f", key="cal_min_ratio")
    with c3:
        always_cost_ratio_target = st.number_input("상시 목표 원가율(캘)", min_value=0.05, max_value=0.95, value=0.18, step=0.01, format="%.2f", key="cal_always_ratio")
        always_list_disc = st.slider("상시할인율(MSRP 대비) %(캘)", 0, 80, 20, 1, key="cal_list_disc") / 100.0
    with c4:
        min_cm = st.slider("최소 기여이익률(%) (캘)", 0, 50, 15, 1, key="cal_cm") / 100.0

    tol = st.slider("일치 허용오차(±%)", 1, 20, 5, 1, key="cal_tol") / 100.0
    policy_for_cal = st.selectbox("캘리브레이션/검증에 사용할 정책", [p["policy"] for p in POLICIES], index=2, key="cal_policy")

    st.markdown("**현재 Disc 테이블(세트타입×가격영역)**")
    st.session_state["set_disc_df"] = st.data_editor(st.session_state["set_disc_df"], use_container_width=True, height=260, num_rows="dynamic", key="disc_editor")

    if st.session_state["products_df"].empty:
        st.warning("먼저 원가/상품마스터를 업로드해야 캘리브레이션이 가능합니다.")
    else:
        if st.button("✅ 캘리브레이션 실행(Disc 자동 채움)", type="primary"):
            sku_always_pred = compute_predicted_sku_always(
                products_df=st.session_state["products_df"],
                channels_df=st.session_state["channels_df"],
                zone_map=st.session_state["zone_map"],
                boundaries=st.session_state["boundaries"],
                rounding_unit=rounding_unit,
                min_cm=min_cm,
                min_cost_ratio_cap=min_cost_ratio_cap,
                always_cost_ratio_target=always_cost_ratio_target,
                always_list_disc=always_list_disc,
                overrides_df=st.session_state["overrides_df"],
                policy=policy_for_cal,
                market_df=st.session_state["market_df"],
            )
            new_disc, obs = calibrate_set_disc_from_history(
                set_df=set_hist,
                bom_df_hist=bom_hist,
                products_df=st.session_state["products_df"],
                sku_always_pred=sku_always_pred,
                params=st.session_state["set_params"],
                disc_df=st.session_state["set_disc_df"]
            )
            st.session_state["set_disc_df"] = new_disc
            st.session_state["cal_obs_df"] = obs
            st.success("Disc 테이블 업데이트 완료")

        with st.expander("역산 로그(Disc_obs / base_disc)", expanded=False):
            obs = st.session_state.get("cal_obs_df", pd.DataFrame())
            st.dataframe(obs.sort_values(["set_type","zone"]).head(300) if not obs.empty else obs, use_container_width=True, height=260)

        st.divider()
        st.subheader("검증: 예측 vs 실제(세트) 일치율")
        if set_hist.empty or bom_hist.empty:
            st.info("세트/구성 데이터가 없습니다. 운영 가격표 업로드 후 실행하세요.")
        else:
            if st.button("📊 검증 실행", type="secondary", key="run_validate_sets"):
                sku_always_pred = compute_predicted_sku_always(
                    products_df=st.session_state["products_df"],
                    channels_df=st.session_state["channels_df"],
                    zone_map=st.session_state["zone_map"],
                    boundaries=st.session_state["boundaries"],
                    rounding_unit=rounding_unit,
                    min_cm=min_cm,
                    min_cost_ratio_cap=min_cost_ratio_cap,
                    always_cost_ratio_target=always_cost_ratio_target,
                    always_list_disc=always_list_disc,
                    overrides_df=st.session_state["overrides_df"],
                    policy=policy_for_cal,
                    market_df=st.session_state["market_df"],
                )

                bom_app = bom_hist.rename(columns={"set_id":"세트ID"})[["세트ID","품번","수량"]].copy()

                rows = []
                actual_cols = ["공구가","홈쇼핑","폐쇄몰","모바일방송가","원데이특가","브랜드위크가","오프라인","상시할인가"]
                for _, sr in set_hist.iterrows():
                    sid = sr["set_id"]
                    anchors = compute_set_anchors(
                        set_id=sid,
                        bom_df=bom_app,
                        products_df=st.session_state["products_df"],
                        sku_always=sku_always_pred,
                        params=st.session_state["set_params"],
                        policy=policy_for_cal,
                        market_df=st.session_state["market_df"],
                        channels_df=st.session_state["channels_df"],
                        zone_map=st.session_state["zone_map"],
                        boundaries=st.session_state["boundaries"],
                        rounding_unit=rounding_unit,
                        min_cm=min_cm,
                        min_cost_ratio_cap=min_cost_ratio_cap,
                        always_cost_ratio_target=always_cost_ratio_target,
                        always_list_disc=always_list_disc,
                    )
                    if anchors is None:
                        continue
                    cost_total = compute_set_cost(sid, bom_app, st.session_state["products_df"], anchors["pack_cost"])
                    if cost_total != cost_total:
                        continue

                    min_auto, max_auto, _ = compute_set_range(
                        cost_total=cost_total,
                        anchors=anchors,
                        channels_df=st.session_state["channels_df"],
                        zone_map=st.session_state["zone_map"],
                        boundaries=st.session_state["boundaries"],
                        rounding_unit=rounding_unit,
                        min_cm=min_cm,
                        min_cost_ratio_cap=min_cost_ratio_cap,
                        always_cost_ratio_target=always_cost_ratio_target,
                        always_list_disc=always_list_disc,
                        policy=policy_for_cal,
                    )

                    zdf = build_zone_table_set(
                        cost_total=float(cost_total),
                        min_price=float(min_auto),
                        max_price=float(max_auto),
                        anchors=anchors,
                        channels_df=st.session_state["channels_df"],
                        zone_map=st.session_state["zone_map"],
                        boundaries=st.session_state["boundaries"],
                        rounding_unit=rounding_unit,
                        min_cm=min_cm,
                        overrides_df=st.session_state["overrides_df"],
                        disc_df=st.session_state["set_disc_df"],
                        params=st.session_state["set_params"],
                        item_id=sid
                    )
                    if zdf.empty:
                        continue

                    for col in actual_cols:
                        actual = safe_float(sr.get(col, np.nan), np.nan)
                        if actual != actual or actual <= 0:
                            continue
                        zone = zone_from_history_column(col)
                        pr = zdf[zdf["가격영역"]==zone]
                        if pr.empty:
                            continue
                        pred = float(pr.iloc[0]["최종가격(원)"])
                        err_pct = abs(pred - actual) / max(1.0, actual)
                        rows.append({
                            "set_id": sid,
                            "set_name": sr.get("set_name",""),
                            "set_type": anchors.get("set_type",""),
                            "zone": zone,
                            "actual": actual,
                            "pred": pred,
                            "err_pct": err_pct,
                            "match": err_pct <= tol,
                        })

                cmp_df = pd.DataFrame(rows)
                if cmp_df.empty:
                    st.error("비교 가능한 데이터가 없습니다. (세트 가격 컬럼이 비어있거나 매핑 문제일 수 있음)")
                    st.session_state["validation_export"] = None
                else:
                    overall = float(cmp_df["match"].mean()) * 100.0
                    st.metric("전체 일치율", f"{overall:.1f}% (N={len(cmp_df):,})")

                    by_zone = cmp_df.groupby("zone", as_index=False).agg(N=("match","size"), Acc=("match","mean"), MAPE=("err_pct","mean"))
                    by_zone["Acc(%)"] = (by_zone["Acc"]*100).round(1)
                    by_zone["MAPE(%)"] = (by_zone["MAPE"]*100).round(1)
                    st.markdown("**가격영역별**")
                    st.dataframe(by_zone.sort_values("N", ascending=False)[["zone","N","Acc(%)","MAPE(%)"]], use_container_width=True, height=260)

                    by_type = cmp_df.groupby("set_type", as_index=False).agg(N=("match","size"), Acc=("match","mean"), MAPE=("err_pct","mean"))
                    by_type["Acc(%)"] = (by_type["Acc"]*100).round(1)
                    by_type["MAPE(%)"] = (by_type["MAPE"]*100).round(1)
                    st.markdown("**세트타입별**")
                    st.dataframe(by_type.sort_values("N", ascending=False)[["set_type","N","Acc(%)","MAPE(%)"]], use_container_width=True, height=180)

                    st.markdown("**오차 큰 TOP 30**")
                    st.dataframe(cmp_df.sort_values("err_pct", ascending=False).head(30), use_container_width=True, height=320)

                    xb, xb_ext, xb_mime = to_excel_bytes({"pred_vs_actual": cmp_df, "by_zone": by_zone, "by_type": by_type})
                    st.session_state["validation_export"] = (xb, xb_ext, xb_mime)

            # download button (safe)
            exp = st.session_state.get("validation_export", None)
            if exp is not None:
                xb, xb_ext, xb_mime = exp
                st.download_button("검증 결과 다운로드", xb, file_name=f"validation_sets.{xb_ext}", mime=xb_mime)
            else:
                st.info("검증 실행 후 결과 다운로드가 활성화됩니다.")


# Add tab_bulk UI code
with tab_bulk:
    st.subheader("SKU 일괄 처리: 엑셀 업로드 → 전체 가격표 + FOB 역산")
    
    c1,c2,c3,c4 = st.columns(4)
    with c1:
        bulk_round = st.selectbox("반올림 단위", [10,100,1000], index=2, key="bulk_round")
    with c2:
        bulk_min_ratio = st.number_input("최저가 원가율 상한", 0.05, 0.95, 0.30, 0.01, "%.2f", key="bulk_min_ratio")
    with c3:
        bulk_always_ratio = st.number_input("상시 목표 원가율", 0.05, 0.95, 0.18, 0.01, "%.2f", key="bulk_always_ratio")
        bulk_list_disc = st.slider("상시할인율(%)", 0, 80, 20, 1, key="bulk_list_disc") / 100.0
    with c4:
        bulk_cm = st.slider("최소 기여이익률(%)", 0, 50, 15, 1, key="bulk_cm") / 100.0
    
    bulk_policy = st.selectbox("적용 정책", [p["policy"] for p in POLICIES], index=2, key="bulk_policy")
    
    st.divider()
    
    bulk_file = st.file_uploader("SKU 일괄 엑셀 업로드 (.xlsx)", type=["xlsx","xls"], key="bulk_upload")
    
    bulk_template_cols = ["품번","상품명","브랜드","카테고리","원가(VAT-)","소비자가(VAT-)","RRP산정방식","해외공식가","통화","환율","관세율(%)","시장유형","동일상품_시장가","경쟁카테고리_앵커가"]
    bulk_tpl_df = pd.DataFrame(columns=bulk_template_cols)
    tpl_bytes, tpl_ext, tpl_mime = to_excel_bytes({"일괄입력": bulk_tpl_df})
    st.download_button("일괄처리 템플릿 다운로드", tpl_bytes, f"bulk_template.{tpl_ext}", tpl_mime)
    
    if bulk_file is not None:
        try:
            bulk_df = pd.read_excel(bulk_file)
            bulk_df.columns = [str(c).strip() for c in bulk_df.columns]
            st.success(f"업로드 완료: {len(bulk_df):,}행")
            
            if st.button("일괄 계산 실행", type="primary", key="run_bulk"):
                fwd_df, rev_df, warn_df = process_bulk_skus(
                    bulk_df=bulk_df,
                    channels_df=st.session_state["channels_df"],
                    zone_map=st.session_state["zone_map"],
                    boundaries=st.session_state["boundaries"],
                    target_pos=st.session_state["target_pos"],
                    overrides_df=st.session_state["overrides_df"],
                    rounding_unit=bulk_round,
                    min_cm=bulk_cm,
                    min_cost_ratio_cap=bulk_min_ratio,
                    always_cost_ratio_target=bulk_always_ratio,
                    always_list_disc=bulk_list_disc,
                    policy=bulk_policy,
                    market_df=st.session_state["market_df"],
                )
                
                st.session_state["bulk_forward"] = fwd_df
                st.session_state["bulk_reverse"] = rev_df
                st.session_state["bulk_warnings"] = warn_df
                st.success("계산 완료")
        except Exception as e:
            st.error(f"업로드 오류: {e}")
    
    fwd = st.session_state.get("bulk_forward", pd.DataFrame())
    rev = st.session_state.get("bulk_reverse", pd.DataFrame())
    wrn = st.session_state.get("bulk_warnings", pd.DataFrame())
    
    if not fwd.empty:
        st.markdown("### 순방향: SKU별 채널 가격표")
        st.dataframe(fwd, use_container_width=True, height=400)
    
    if not rev.empty:
        st.markdown("### 역산: FOB 테이블")
        st.dataframe(rev, use_container_width=True, height=400)
    
    if not wrn.empty:
        st.markdown("### 경고/이슈")
        st.dataframe(wrn, use_container_width=True, height=200)
    
    if not fwd.empty or not rev.empty:
        sheets = {}
        if not fwd.empty: sheets["순방향_가격표"] = fwd
        if not rev.empty: sheets["역산_FOB"] = rev
        if not wrn.empty: sheets["경고_이슈"] = wrn
        xb, xb_ext, xb_mime = to_excel_bytes(sheets)
        st.download_button("결과 다운로드", xb, f"bulk_result.{xb_ext}", xb_mime, key="bulk_download")

with tab_reverse:
    st.subheader("역산 원가 시뮬레이터 — 소비자가에서 브랜드사 공급가(FOB) 역산")
    
    st.markdown("#### 1. 소비자가(RRP) 산정 — VAT 별도")
    rrp_case = st.radio("산정 방식", ["직접입력", "국내공식가×0.9", "해외공식가×환율×1.1", "경쟁사조사역산"],
                        horizontal=True, key="rev_rrp_case")
    
    rev_rrp = 0
    rev_rrp_note = ""
    
    if rrp_case == "직접입력":
        rev_direct = st.number_input("소비자가 (원, VAT별도)", min_value=0, value=50000, step=1000, key="rev_direct")
        rev_rrp = int(rev_direct)
        rev_rrp_note = "직접 입력"
    
    elif rrp_case == "국내공식가×0.9":
        rev_domestic = st.number_input("국내 공식가 (원, VAT별도)", min_value=0, value=50000, step=1000, key="rev_domestic")
        rev_rrp = krw_round(rev_domestic * 0.9, 100)
        rev_rrp_note = f"국내공식가 {int(rev_domestic):,}원 × 0.9"
    
    elif rrp_case == "해외공식가×환율×1.1":
        rc1, rc2 = st.columns(2)
        with rc1:
            rev_cur_idx = st.selectbox("통화", range(len(CURRENCIES)),
                                       format_func=lambda i: f"{CURRENCIES[i]['code']} ({CURRENCIES[i]['name']})", key="rev_cur")
            rev_cur = CURRENCIES[rev_cur_idx]
        with rc2:
            rev_er = st.number_input(f"환율 (₩/{rev_cur['code']})", value=float(rev_cur["default_rate"]), step=10.0, key="rev_er")
        rev_overseas = st.number_input(f"해외 공식가 ({rev_cur['code']})", value=30.0, step=0.5, key="rev_overseas")
        rev_rrp = int(np.ceil(rev_overseas * rev_er * 1.1 / 1000) * 1000)
        rev_rrp_note = f"{rev_overseas} × {rev_er:,.0f} × 1.1 → 천원 반올림"
    
    else:
        rc1, rc2 = st.columns(2)
        with rc1:
            rev_comp_avg = st.number_input("경쟁사 평균 판매가 (원)", min_value=0, value=46500, step=1000, key="rev_comp")
        with rc2:
            rev_disc_coeff = st.number_input("상시할인가 계수", value=0.929, step=0.01, format="%.3f", key="rev_coeff")
        rev_rrp = int(np.ceil(rev_comp_avg / rev_disc_coeff / 1000) * 1000)
        rev_rrp_note = f"경쟁사 평균 {rev_comp_avg:,} ÷ {rev_disc_coeff}"
    
    if rev_rrp > 0:
        st.info(f"기준 판매가: **{rev_rrp:,}원** ({rev_rrp_note})")
    
    st.divider()
    
    st.markdown("#### 2. 변수 입력")
    vc1, vc2, vc3, vc4 = st.columns(4)
    with vc1:
        if rrp_case != "해외공식가×환율×1.1":
            rev_cur_idx2 = st.selectbox("결과 통화", range(len(CURRENCIES)),
                                        format_func=lambda i: f"{CURRENCIES[i]['code']} ({CURRENCIES[i]['name']})", key="rev_cur2")
            rev_cur_final = CURRENCIES[rev_cur_idx2]
            rev_er_final = st.number_input(f"환율 (₩/{rev_cur_final['code']})", value=float(rev_cur_final["default_rate"]), step=10.0, key="rev_er2")
        else:
            rev_cur_final = rev_cur
            rev_er_final = rev_er
    with vc2:
        rev_tariff = st.number_input("관세율 (%)", min_value=0.0, value=8.0, step=0.5, key="rev_tariff")
    with vc3:
        rev_category = st.selectbox("카테고리", list(CATEGORY_TARGETS.keys()), key="rev_cat")
    with vc4:
        rev_foreign_official = st.number_input(f"해외 공식가 ({rev_cur_final['code']}, 비교용)", value=0.0, step=0.5, key="rev_fop")
    
    if rev_rrp > 0:
        st.divider()
        st.markdown(f"#### 3. 원가율별 최대 FOB ({rev_cur_final['code']})")
        
        cat_info = CATEGORY_TARGETS.get(rev_category, {"target": 0.25, "max": 0.30})
        tariff_dec = rev_tariff / 100.0
        
        cols = st.columns(4)
        rev_results = []
        for i, tier in enumerate(REVERSE_TIERS):
            r = calc_reverse_fob(rev_rrp, tier["rate"], rev_er_final, tariff_dec)
            ratio_to_official = None
            if rev_foreign_official > 0 and r["fob_krw"] == r["fob_krw"]:
                ratio_to_official = r["fob_krw"] / (rev_foreign_official * rev_er_final) * 100
            rev_results.append({**tier, **r, "ratio_to_official": ratio_to_official})
            
            is_target = abs(tier["rate"] - cat_info["target"]) < 0.02
            with cols[i]:
                if is_target:
                    st.markdown(f"**🎯 카테고리 목표**")
                st.markdown(f"**{tier['label']}** (원가율 {int(tier['rate']*100)}%)")
                st.markdown(f"*{tier['desc']}*")
                st.metric("랜딩코스트", f"{int(r['landing_cost']):,}원" if r['landing_cost']==r['landing_cost'] else "—")
                st.metric(f"최대 FOB ({rev_cur_final['code']})",
                         f"{rev_cur_final['symbol']}{r['fob']:.2f}" if r['fob']==r['fob'] else "—")
        
        detail_rows = []
        for rr in rev_results:
            row = {
                "원가율": f"{int(rr['rate']*100)}%",
                "판단": rr["label"],
                "랜딩코스트(원)": int(rr["landing_cost"]) if rr["landing_cost"]==rr["landing_cost"] else np.nan,
                f"FOB({rev_cur_final['code']})": f"{rev_cur_final['symbol']}{rr['fob']:.2f}" if rr['fob']==rr['fob'] else "—",
            }
            detail_rows.append(row)
        st.dataframe(pd.DataFrame(detail_rows), use_container_width=True, hide_index=True)

