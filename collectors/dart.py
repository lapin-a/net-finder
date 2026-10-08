"""OpenDART(금융감독원 전자공시) 수집기.

API 키 발급: https://opendart.fss.or.kr
"""
import hashlib
import io
import json
import time
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

BASE_URL = "https://opendart.fss.or.kr/api"


class DartClient:
    def __init__(self, api_key: str, delay: float = 0.2, cache_dir: Path | None = None):
        """cache_dir를 주면 JSON 응답을 파일로 저장해 두고 같은 요청은 다시 호출하지 않는다."""
        if not api_key:
            raise ValueError("DART_API_KEY가 설정되지 않았습니다.")
        self.api_key = api_key
        self.delay = delay
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.session = requests.Session()

    def _get(self, path: str, retries: int = 4, **params) -> requests.Response:
        params["crtfc_key"] = self.api_key
        for attempt in range(retries + 1):
            try:
                resp = self.session.get(f"{BASE_URL}/{path}", params=params, timeout=60)
                resp.raise_for_status()
                break
            except (requests.ConnectionError, requests.Timeout):
                if attempt == retries:
                    raise
                time.sleep(2 ** (attempt + 1))  # 2, 4, 8, 16초 대기 후 재시도
        time.sleep(self.delay)
        return resp

    def _get_json(self, path: str, **params) -> dict:
        cache = None
        if self.cache_dir:
            key = json.dumps([path, sorted(params.items())], ensure_ascii=False)
            cache = self.cache_dir / f"{hashlib.md5(key.encode()).hexdigest()}.json"
            if cache.exists():
                return json.loads(cache.read_text(encoding="utf-8"))

        data = self._get(path, **params).json()
        # 000: 정상, 013: 조회된 데이터 없음
        if data.get("status") not in ("000", "013"):
            raise RuntimeError(f"DART 오류 {data.get('status')}: {data.get('message')}")

        if cache:  # 오류 응답(한도 초과 등)은 저장하지 않음
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data

    def corp_codes(self) -> list[dict]:
        """전체 회사 고유번호 목록 (corp_code, corp_name, stock_code)."""
        resp = self._get("corpCode.xml")
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            root = ET.fromstring(zf.read("CORPCODE.xml"))
        return [
            {
                "corp_code": item.findtext("corp_code"),
                "corp_name": item.findtext("corp_name"),
                "stock_code": (item.findtext("stock_code") or "").strip(),
                "modify_date": item.findtext("modify_date"),
            }
            for item in root.iter("list")
        ]

    def filings(self, corp_code: str, bgn_de: str, end_de: str, page_count: int = 100) -> list[dict]:
        """공시 목록 검색. 날짜 형식: YYYYMMDD."""
        results, page = [], 1
        while True:
            data = self._get_json(
                "list.json",
                corp_code=corp_code,
                bgn_de=bgn_de,
                end_de=end_de,
                page_no=page,
                page_count=page_count,
            )
            results.extend(data.get("list", []))
            if page >= int(data.get("total_page", 1)):
                return results
            page += 1

    def financial_statements(self, corp_code: str, year: str, reprt_code: str = "11011", fs_div: str = "CFS") -> list[dict]:
        """단일회사 전체 재무제표.

        reprt_code: 11013 1분기, 11012 반기, 11014 3분기, 11011 사업보고서
        fs_div: CFS 연결, OFS 별도
        """
        data = self._get_json(
            "fnlttSinglAcntAll.json",
            corp_code=corp_code,
            bsns_year=year,
            reprt_code=reprt_code,
            fs_div=fs_div,
        )
        return data.get("list", [])

    def multi_major_accounts(self, corp_codes: list[str], year: str, reprt_code: str = "11011") -> list[dict]:
        """다중회사 주요계정 (최대 100개 회사). 유동자산, 부채총계, 당기순이익 등 연결·별도 모두."""
        data = self._get_json(
            "fnlttMultiAcnt.json",
            corp_code=",".join(corp_codes),
            bsns_year=year,
            reprt_code=reprt_code,
        )
        return data.get("list", [])

    def treasury_stock(self, corp_code: str, year: str, reprt_code: str = "11011") -> list[dict]:
        """자기주식 취득 및 처분 현황 (기초, 취득, 처분, 소각, 기말 수량). 연초부터 누적."""
        data = self._get_json(
            "tesstkAcqsDspsSttus.json",
            corp_code=corp_code,
            bsns_year=year,
            reprt_code=reprt_code,
        )
        return data.get("list", [])

    def share_counts(self, corp_code: str, year: str, reprt_code: str = "11011") -> list[dict]:
        """주식의 총수 현황 (발행주식총수, 자기주식수, 유통주식수)."""
        data = self._get_json(
            "stockTotqySttus.json",
            corp_code=corp_code,
            bsns_year=year,
            reprt_code=reprt_code,
        )
        return data.get("list", [])
