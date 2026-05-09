from __future__ import annotations

import json
import ssl
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_TIMEOUT_SECONDS = 30
SENSITIVE_QUERY_KEYS = {"api_key", "apikey"}


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
        self._ssl_context = ssl.create_default_context()

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
            body = exc.read()
            raise RuntimeError(
                f"HTTP {exc.code} for {safe_full_url}: {body.decode('utf-8', errors='ignore')}"
            ) from exc
        except URLError as exc:  # pragma: no cover - exercised via integration paths
            raise RuntimeError(f"Network error for {safe_full_url}: {exc.reason}") from exc


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
