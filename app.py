from io import BytesIO
from zipfile import ZipFile
import xml.etree.ElementTree as ET

import pandas as pd
import plotly.express as px
import requests
import streamlit as st


st.set_page_config(
    page_title="기업 재무 비교 플랫폼",
    page_icon="📊",
    layout="wide",
)

CORP_CODE_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
FINANCIAL_URL = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json"


def get_api_key() -> str:
    """Streamlit Secrets에서 Open DART 인증키를 가져온다."""
    try:
        return st.secrets["DART_API_KEY"]
    except (KeyError, FileNotFoundError):
        st.error(
            "Open DART 인증키가 설정되지 않았습니다. "
            "Streamlit 앱 설정의 Secrets에 인증키를 등록해 주세요."
        )
        st.stop()


@st.cache_data(ttl=86400, show_spinner=False)
def load_corporations(api_key: str) -> pd.DataFrame:
    """Open DART 기업 고유번호 목록을 내려받는다."""
    response = requests.get(
        CORP_CODE_URL,
        params={"crtfc_key": api_key},
        timeout=30,
    )
    response.raise_for_status()

    try:
        with ZipFile(BytesIO(response.content)) as zip_file:
            xml_name = zip_file.namelist()[0]
            xml_data = zip_file.read(xml_name)
    except Exception as error:
        raise RuntimeError(
            "기업 목록 파일을 해석하지 못했습니다. 인증키를 확인해 주세요."
        ) from error

    root = ET.fromstring(xml_data)
    records = []

    for item in root.findall("list"):
        corp_name = item.findtext("corp_name", "").strip()
        corp_code = item.findtext("corp_code", "").strip()
        stock_code = item.findtext("stock_code", "").strip()

        if corp_name and corp_code:
            records.append(
                {
                    "기업명": corp_name,
                    "고유번호": corp_code,
                    "종목코드": stock_code,
                }
            )

    if not records:
        raise RuntimeError("기업 목록을 불러오지 못했습니다.")

    return pd.DataFrame(records)


def search_company(
    companies: pd.DataFrame,
    keyword: str,
) -> pd.DataFrame:
    """기업명 검색 결과를 반환한다."""
    keyword = keyword.strip()

    if not keyword:
        return companies.iloc[0:0]

    exact = companies[
        companies["기업명"].str.lower() == keyword.lower()
    ]

    contains = companies[
        companies["기업명"].str.contains(
            keyword,
            case=False,
            na=False,
            regex=False,
        )
    ]

    return (
        pd.concat([exact, contains])
        .drop_duplicates(subset=["고유번호"])
        .head(20)
    )


@st.cache_data(ttl=3600, show_spinner=False)
def load_financial_statements(
    api_key: str,
    corp_code: str,
    business_year: int,
) -> pd.DataFrame:
    """기업의 연결재무제표를 불러온다."""
    params = {
        "crtfc_key": api_key,
        "corp_code": corp_code,
        "bsns_year": str(business_year),
        "reprt_code": "11011",
        "fs_div": "CFS",
    }

    response = requests.get(
        FINANCIAL_URL,
        params=params,
        timeout=30,
    )
    response.raise_for_status()

    result = response.json()

    if result.get("status") != "000":
        raise RuntimeError(
            result.get("message", "재무정보를 불러오지 못했습니다.")
        )

    data = pd.DataFrame(result.get("list", []))

    if data.empty:
        raise RuntimeError("해당 연도의 재무정보가 없습니다.")

    return data


def clean_amount(value: object) -> float | None:
    """DART 금액 문자열을 숫자로 변환한다."""
    if value is None or pd.isna(value):
        return None

    text = str(value).replace(",", "").strip()

    if not text or text == "-":
        return None

    try:
        return float(text)
    except ValueError:
        return None


def find_account(
    data: pd.DataFrame,
    account_names: list[str],
) -> float | None:
    """여러 계정명 후보 중 일치하는 값을 찾는다."""
    if "account_nm" not in data.columns:
        return None

    for name in account_names:
        exact = data[data["account_nm"].str.strip() == name]

        if not exact.empty:
            value = clean_amount(exact.iloc[0].get("thstrm_amount"))
            if value is not None:
                return value

    for name in account_names:
        partial = data[
            data["account_nm"].str.contains(
                name,
                case=False,
                na=False,
                regex=False,
            )
        ]

        if not partial.empty:
            value = clean_amount(partial.iloc[0].get("thstrm_amount"))
            if value is not None:
                return value

    return None


def extract_metrics(data: pd.DataFrame) -> dict[str, float | None]:
    """기업 비교에 필요한 주요 계정을 추출한다."""
    return {
        "매출액": find_account(
            data,
            ["매출액", "수익(매출액)", "영업수익", "매출"],
        ),
        "영업이익": find_account(
            data,
            ["영업이익", "영업이익(손실)", "영업손익"],
        ),
        "당기순이익": find_account(
            data,
            [
                "당기순이익",
                "당기순이익(손실)",
                "연결당기순이익",
                "분기순이익",
            ],
        ),
        "자산총계": find_account(
            data,
            ["자산총계"],
        ),
        "부채총계": find_account(
            data,
            ["부채총계"],
        ),
        "자본총계": find_account(
            data,
            ["자본총계"],
        ),
    }


def safe_ratio(
    numerator: float | None,
    denominator: float | None,
) -> float | None:
    """분모가 없거나 0일 때 계산을 중단한다."""
    if numerator is None or denominator in (None, 0):
        return None

    return numerator / denominator * 100


def format_won(value: float | None) -> str:
    """원 단위 값을 억 원 단위로 표시한다."""
    if value is None:
        return "자료 없음"

    return f"{value / 100_000_000:,.0f}억 원"


def format_percent(value: float | None) -> str:
    if value is None:
        return "계산 불가"

    return f"{value:,.2f}%"


def create_analysis(
    company_name: str,
    metrics: dict[str, float | None],
) -> str:
    """계산된 지표를 바탕으로 설명 가능한 분석문을 만든다."""
    margin = safe_ratio(
        metrics["영업이익"],
        metrics["매출액"],
    )

    debt_to_assets = safe_ratio(
        metrics["부채총계"],
        metrics["자산총계"],
    )

    comments = []

    if margin is None:
        comments.append("영업이익률을 계산할 자료가 부족합니다.")
    elif margin >= 10:
        comments.append("영업이익률이 10% 이상으로 나타났습니다.")
    elif margin >= 5:
        comments.append("영업이익률이 5~10% 구간으로 나타났습니다.")
    else:
        comments.append("영업이익률이 5% 미만으로 나타났습니다.")

    if debt_to_assets is None:
        comments.append("자산 대비 부채 비중은 계산하지 못했습니다.")
    elif debt_to_assets <= 40:
        comments.append("자산 대비 부채 비중이 40% 이하입니다.")
    elif debt_to_assets <= 60:
        comments.append("자산 대비 부채 비중이 40~60% 구간입니다.")
    else:
        comments.append("자산 대비 부채 비중이 60%를 초과합니다.")

    comments.append(
        "다만 업종과 기업 규모가 다르면 동일한 수치만으로 "
        "우열을 판단하기 어렵습니다."
    )

    return f"**{company_name}**: " + " ".join(comments)


st.title("📊 기업 재무정보 비교 분석 플랫폼")

st.write(
    "비교하고 싶은 국내 공시대상 기업 두 곳을 검색하면 "
    "Open DART의 재무정보를 불러와 비교합니다."
)

st.info(
    "현재 버전은 재무정보 비교 기능을 제공합니다. "
    "ESG 및 생성형 AI 분석 기능은 이후 확장할 수 있습니다."
)

api_key = get_api_key()

try:
    with st.spinner("Open DART 기업 목록을 불러오는 중입니다."):
        companies = load_corporations(api_key)
except (requests.RequestException, RuntimeError) as error:
    st.error(str(error))
    st.stop()

business_year = st.selectbox(
    "사업연도",
    options=[2025, 2024, 2023, 2022],
    index=1,
)

left, right = st.columns(2)

with left:
    keyword_a = st.text_input(
        "기업 A 검색",
        placeholder="예: 농심",
    )
    results_a = search_company(companies, keyword_a)

    if not results_a.empty:
        company_a_name = st.selectbox(
            "기업 A 선택",
            results_a["기업명"].tolist(),
            key="company_a",
        )
    else:
        company_a_name = None

with right:
    keyword_b = st.text_input(
        "기업 B 검색",
        placeholder="예: 롯데칠성음료",
    )
    results_b = search_company(companies, keyword_b)

    if not results_b.empty:
        company_b_name = st.selectbox(
            "기업 B 선택",
            results_b["기업명"].tolist(),
            key="company_b",
        )
    else:
        company_b_name = None

compare_clicked = st.button(
    "두 기업 비교하기",
    type="primary",
    use_container_width=True,
)

if compare_clicked:
    if not company_a_name or not company_b_name:
        st.warning("기업 두 곳을 모두 검색하고 선택해 주세요.")
        st.stop()

    if company_a_name == company_b_name:
        st.warning("서로 다른 기업을 선택해 주세요.")
        st.stop()

    corp_a = results_a[
        results_a["기업명"] == company_a_name
    ].iloc[0]

    corp_b = results_b[
        results_b["기업명"] == company_b_name
    ].iloc[0]

    try:
        with st.spinner("재무정보를 불러오고 있습니다."):
            financial_a = load_financial_statements(
                api_key,
                corp_a["고유번호"],
                business_year,
            )

            financial_b = load_financial_statements(
                api_key,
                corp_b["고유번호"],
                business_year,
            )
    except (requests.RequestException, RuntimeError) as error:
        st.error(str(error))
        st.stop()

    metrics_a = extract_metrics(financial_a)
    metrics_b = extract_metrics(financial_b)

    operating_margin_a = safe_ratio(
        metrics_a["영업이익"],
        metrics_a["매출액"],
    )
    operating_margin_b = safe_ratio(
        metrics_b["영업이익"],
        metrics_b["매출액"],
    )

    debt_ratio_a = safe_ratio(
        metrics_a["부채총계"],
        metrics_a["자산총계"],
    )
    debt_ratio_b = safe_ratio(
        metrics_b["부채총계"],
        metrics_b["자산총계"],
    )

    st.subheader(f"{business_year}년 기업 비교")

    comparison = pd.DataFrame(
        {
            "비교 항목": [
                "매출액",
                "영업이익",
                "당기순이익",
                "자산총계",
                "부채총계",
                "자본총계",
                "영업이익률",
                "자산 대비 부채 비중",
            ],
            company_a_name: [
                format_won(metrics_a["매출액"]),
                format_won(metrics_a["영업이익"]),
                format_won(metrics_a["당기순이익"]),
                format_won(metrics_a["자산총계"]),
                format_won(metrics_a["부채총계"]),
                format_won(metrics_a["자본총계"]),
                format_percent(operating_margin_a),
                format_percent(debt_ratio_a),
            ],
            company_b_name: [
                format_won(metrics_b["매출액"]),
                format_won(metrics_b["영업이익"]),
                format_won(metrics_b["당기순이익"]),
                format_won(metrics_b["자산총계"]),
                format_won(metrics_b["부채총계"]),
                format_won(metrics_b["자본총계"]),
                format_percent(operating_margin_b),
                format_percent(debt_ratio_b),
            ],
        }
    )

    st.dataframe(
        comparison,
        use_container_width=True,
        hide_index=True,
    )

    chart_rows = []

    for account in ["매출액", "영업이익", "당기순이익"]:
        for company_name, metrics in [
            (company_a_name, metrics_a),
            (company_b_name, metrics_b),
        ]:
            value = metrics[account]

            if value is not None:
                chart_rows.append(
                    {
                        "기업": company_name,
                        "항목": account,
                        "금액(억 원)": value / 100_000_000,
                    }
                )

    if chart_rows:
        chart_data = pd.DataFrame(chart_rows)

        figure = px.bar(
            chart_data,
            x="항목",
            y="금액(억 원)",
            color="기업",
            barmode="group",
            title="주요 손익 항목 비교",
        )

        st.plotly_chart(
            figure,
            use_container_width=True,
        )

    st.subheader("지표 기반 분석")

    analysis_left, analysis_right = st.columns(2)

    with analysis_left:
        st.markdown(
            create_analysis(
                company_a_name,
                metrics_a,
            )
        )

    with analysis_right:
        st.markdown(
            create_analysis(
                company_b_name,
                metrics_b,
            )
        )

    st.caption(
        "자료 출처: 금융감독원 Open DART. "
        "계정명과 공시 형태에 따라 일부 항목이 표시되지 않을 수 있습니다. "
        "이 결과는 교육용 비교이며 투자 권유가 아닙니다."
    )
