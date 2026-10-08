"""SEC EDGAR 수집기.

API 키는 필요 없지만 User-Agent에 이름과 이메일을 반드시 넣어야 하며,
초당 10회 이하로 요청해야 합니다. https://www.sec.gov/os/accessing-edgar-data
"""
import re
import time

import requests

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
DATA_URL = "https://data.sec.gov"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data"
LINKBASE_FILE = re.compile(r"(_(cal|def|lab|pre)\.xml|FilingSummary\.xml)$", re.IGNORECASE)


class EdgarClient:
    def __init__(self, user_agent: str, delay: float = 0.15):
        if not user_agent:
            raise ValueError("EDGAR_USER_AGENT가 설정되지 않았습니다. 예: 'Your Name you@example.com'")
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})

    def _get(self, url: str, timeout: int = 30) -> requests.Response:
        for attempt in range(5):
            try:
                resp = self.session.get(url, timeout=timeout)
                if resp.status_code in (429, 503):  # 요청 과다 → 잠시 쉬고 재시도
                    raise requests.ConnectionError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                break
            except (requests.ConnectionError, requests.Timeout):
                if attempt == 4:
                    raise
                time.sleep(2 ** (attempt + 1))
        time.sleep(self.delay)
        return resp

    def _get_json(self, url: str) -> dict:
        return self._get(url).json()

    def tickers(self) -> list[dict]:
        """전체 티커 목록 (ticker, name, cik)."""
        return [
            {"ticker": row["ticker"], "name": row["title"], "cik": str(row["cik_str"]).zfill(10)}
            for row in self._get_json(TICKERS_URL).values()
        ]

    def cik_for_ticker(self, ticker: str) -> str:
        ticker = ticker.upper()
        for row in self.tickers():
            if row["ticker"] == ticker:
                return row["cik"]
        raise KeyError(f"티커를 찾을 수 없습니다: {ticker}")

    def submissions(self, cik: str) -> dict:
        """회사 정보 및 최근 공시 목록."""
        return self._get_json(f"{DATA_URL}/submissions/CIK{cik.zfill(10)}.json")

    def filings(self, cik: str, form_types: tuple[str, ...] = (), include_older: bool = False) -> list[dict]:
        """공시 목록을 행 단위 dict 리스트로 변환.

        submissions는 최근 1,000건만 담고 나머지는 별도 파일로 나뉜다. 큰 회사는 내부자 거래 공시가 많아
        최근 1,000건이 2~3년치밖에 안 되므로, 오래된 공시까지 필요하면 include_older=True.
        """
        data = self.submissions(cik)["filings"]
        pages = [data["recent"]]
        if include_older:
            pages += [self._get_json(f"{DATA_URL}/submissions/{f['name']}") for f in data.get("files", [])]
        rows = [dict(zip(page.keys(), values)) for page in pages for values in zip(*page.values())]
        if form_types:
            rows = [r for r in rows if r["form"] in form_types]
        return rows

    def company_facts(self, cik: str) -> dict:
        """XBRL 재무 데이터 전체 (us-gaap 등)."""
        return self._get_json(f"{DATA_URL}/api/xbrl/companyfacts/CIK{cik.zfill(10)}.json")

    def filing_instance(self, cik: str, accession: str) -> bytes:
        """공시 한 건의 XBRL 인스턴스 문서 (회사 자체 항목과 차원(열 구분)이 붙은 값까지 모두 포함).

        인라인 XBRL 공시는 SEC가 추출해 둔 '<이름>_htm.xml', 그 이전 공시는 링크베이스를 뺀 .xml 파일.
        """
        base = f"{ARCHIVES_URL}/{int(cik)}/{accession.replace('-', '')}"
        items = self._get_json(f"{base}/index.json")["directory"]["item"]
        xmls = [i for i in items if i["name"].lower().endswith(".xml") and not LINKBASE_FILE.search(i["name"])]
        extracted = [i for i in xmls if i["name"].endswith("_htm.xml")]
        candidates = extracted or sorted(xmls, key=lambda i: -int(i.get("size") or 0))
        if not candidates:
            raise FileNotFoundError(f"XBRL 인스턴스가 없습니다: {base}")
        return self._get(f"{base}/{candidates[0]['name']}", timeout=120).content
