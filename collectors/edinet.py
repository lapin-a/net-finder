"""EDINET(금융청 전자공시) API v2 수집기.

API 키 발급: https://api.edinet-fsa.go.jp/api/auth/index.aspx?mode=1
"""
import csv
import gzip
import io
import json
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path

import requests

BASE_URL = "https://api.edinet-fsa.go.jp/api/v2"
FACT_FIELDS = ["要素ID", "コンテキストID", "単位", "値"]  # CSV 9열 중 계산에 쓰는 열만 캐시


class EdinetClient:
    def __init__(self, api_key: str, cache_dir: Path | None = None, delay: float = 0.3):
        if not api_key:
            raise ValueError("EDINET_API_KEY가 설정되지 않았습니다.")
        self.api_key = api_key
        self.delay = delay
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.session = requests.Session()

    def _get(self, path: str, retries: int = 4, **params) -> requests.Response:
        params["Subscription-Key"] = self.api_key
        for attempt in range(retries + 1):
            try:
                resp = self.session.get(f"{BASE_URL}/{path}", params=params, timeout=120)
                if resp.status_code < 500:
                    resp.raise_for_status()
                    break
            except (requests.ConnectionError, requests.Timeout):
                if attempt == retries:
                    raise
            if attempt == retries:
                resp.raise_for_status()
            time.sleep(2 ** (attempt + 1))  # 2, 4, 8, 16초 대기 후 재시도
        time.sleep(self.delay)
        return resp

    def documents(self, day: date) -> list[dict]:
        """그날 제출된 서류 목록 (docID, secCode, docTypeCode, periodEnd 등). 최근 3일은 아직 바뀔 수 있어 저장하지 않음."""
        cache = self.cache_dir / f"list/{day.isoformat()}.json" if self.cache_dir else None
        if cache and cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
        data = self._get("documents.json", date=day.isoformat(), type=2).json()
        status = str(data.get("metadata", {}).get("status") or data.get("StatusCode"))
        if status != "200":
            raise RuntimeError(f"EDINET 오류 {status}: {data.get('metadata', {}).get('message') or data.get('message')}")
        results = data.get("results") or []
        if cache and date.today() - day > timedelta(days=3):
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
        return results

    def facts(self, doc_id: str) -> list[dict]:
        """서류의 XBRL→CSV(type=5) 중 본문 CSV(jpcrp·jpsps 등, 감사보고서 jpaud 제외)의 수치·짧은 값 행.
        긴 본문(TextBlock)은 빼고 gzip으로 캐시. 행: {要素ID, コンテキストID, 単位, 値}"""
        cache = self.cache_dir / f"docs/{doc_id}.tsv.gz" if self.cache_dir else None
        if cache and cache.exists():
            with gzip.open(cache, "rt", encoding="utf-8", newline="") as f:
                return list(csv.DictReader(f, delimiter="\t"))

        for attempt in range(5):
            resp = self._get(f"documents/{doc_id}", type=5)
            # 오류는 HTTP 200 + JSON 본문으로 옴. 429(요청 과다)는 쉬었다가 다시, 나머지(서류·CSV 없음)는 오류
            if "application/json" not in resp.headers.get("Content-Type", ""):
                break
            if '"429"' not in resp.text or attempt == 4:
                raise RuntimeError(f"EDINET 서류 {doc_id}: {resp.text[:200]}")
            time.sleep(30 * (attempt + 1))
        rows = []
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            for name in zf.namelist():
                if not name.endswith(".csv") or "/jpaud" in name:
                    continue
                text = zf.read(name).decode("utf-16")
                for r in csv.DictReader(io.StringIO(text), delimiter="\t"):
                    if not r["要素ID"].endswith("TextBlock"):
                        rows.append({k: r[k] for k in FACT_FIELDS})
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(cache, "wt", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=FACT_FIELDS, delimiter="\t")
                writer.writeheader()
                writer.writerows(rows)
        return rows
