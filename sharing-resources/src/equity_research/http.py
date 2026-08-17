from __future__ import annotations

import json
import ssl
import time
import certifi
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_TIMEOUT_SECONDS = 30
SENSITIVE_QUERY_KEYS = {"api_key", "apikey"}
_MAX_HTTP_RETRIES = 3
_RETRY_BACKOFF_SECONDS = (1, 2)  # before 2nd attempt, before 3rd


@dataclass(slots=True)
class HttpResponse:
    url: str
    status: int
    body: bytes

    @property
    def safe_url(self) -> str:
        return redact_url(self.url)

    def json(self) -> dict:
        return json.loads(self.body.decode("utf-8"))

    def text(self) -> str:
        return self.body.decode("utf-8")


class HttpClient:
    def __init__(self, user_agent: str) -> None:
        self._user_agent = user_agent
        self._ssl_context = ssl.create_default_context(cafile=certifi.where())

    def get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> HttpResponse:
        full_url = f"{url}?{urlencode(params)}" if params else url
        safe_full_url = redact_url(full_url)
        request_headers = {"User-Agent": self._user_agent}
        if headers:
            request_headers.update(headers)
        request = Request(full_url, headers=request_headers)

        last_exception = None
        for attempt in range(_MAX_HTTP_RETRIES):
            try:
                with urlopen(
                    request,
                    timeout=timeout_seconds if timeout_seconds is not None else DEFAULT_TIMEOUT_SECONDS,
                    context=self._ssl_context,
                ) as response:
                    return HttpResponse(
                        url=full_url,
                        status=response.status,
                        body=response.read(),
                    )
            except HTTPError as exc:  # pragma: no cover - exercised via integration paths
                last_exception = exc
                if exc.code in {500, 502, 503, 504, 429}:
                    if attempt < _MAX_HTTP_RETRIES - 1:
                        time.sleep(_RETRY_BACKOFF_SECONDS[attempt])
                        continue
                body = exc.read()
                raise RuntimeError(
                    f"HTTP {exc.code} for {safe_full_url}: {body.decode('utf-8', errors='ignore')}"
                ) from exc
            except URLError as exc:  # pragma: no cover - exercised via integration paths
                last_exception = exc
                if attempt < _MAX_HTTP_RETRIES - 1:
                    time.sleep(_RETRY_BACKOFF_SECONDS[attempt])
                    continue
                raise RuntimeError(f"Network error for {safe_full_url}: {exc.reason}") from exc

        # All retries exhausted
        if last_exception:
            if isinstance(last_exception, HTTPError):
                body = last_exception.read()
                raise RuntimeError(
                    f"HTTP {last_exception.code} for {safe_full_url}: {body.decode('utf-8', errors='ignore')}"
                ) from last_exception
            else:
                raise RuntimeError(f"Network error for {safe_full_url}: {last_exception.reason}") from last_exception


def redact_url(url: str) -> str:
    split = urlsplit(url)
    if not split.query:
        return url
    redacted_query = []
    for key, value in parse_qsl(split.query, keep_blank_values=True):
        if key.lower() in SENSITIVE_QUERY_KEYS:
            redacted_query.append((key, "REDACTED"))
        else:
            redacted_query.append((key, value))
    return urlunsplit(
        (
            split.scheme,
            split.netloc,
            split.path,
            urlencode(redacted_query, doseq=True),
            split.fragment,
        )
    )
