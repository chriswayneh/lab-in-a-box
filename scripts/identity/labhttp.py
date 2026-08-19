"""
Minimal JSON-over-HTTP helper.

Standard library only, on purpose. The lifecycle engine runs in a throwaway
`python:3.12-alpine` container, and a `pip install` there would mean a network
fetch on every `make jml-*` invocation — slow, and one more thing to break on a
laptop with no internet.

`urllib` is unpleasant enough to use directly that wrapping it once, here, is
worth it. Everything above this module deals in dicts and exceptions.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request


class HttpError(RuntimeError):
    """A non-2xx response, carrying enough context to explain itself."""

    def __init__(self, method: str, url: str, status: int, body: str):
        self.method = method
        self.url = url
        self.status = status
        self.body = body
        # The URL is safe to show: credentials travel in headers, never in the
        # query string, so this cannot leak one into a log.
        super().__init__(f"{method} {url} -> HTTP {status}: {body[:400]}")


class Unavailable(RuntimeError):
    """The service could not be reached at all (DNS, refused, timeout)."""


def request(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    json_body: object | None = None,
    form_body: dict | None = None,
    timeout: int = 20,
    retries: int = 3,
    expect_json: bool = True,
):
    """
    Perform one HTTP request and return the decoded body.

    Returns a parsed object for a JSON response, the raw string when
    `expect_json` is False, and None for 204/empty bodies.

    Retries only on transport failure and 5xx. A 4xx is a decision the server
    made deliberately -- repeating it just makes the same mistake three times
    and delays the error the operator needs to see.
    """
    headers = dict(headers or {})
    data = None

    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers.setdefault("Content-Type", "application/json")
    elif form_body is not None:
        data = urllib.parse.urlencode(form_body).encode()
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

    headers.setdefault("Accept", "application/json")

    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode(errors="replace")
                if not raw.strip():
                    return None
                if not expect_json:
                    return raw
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return raw

        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            if exc.code >= 500 and attempt < retries:
                last_error = HttpError(method, url, exc.code, body)
                time.sleep(attempt)  # 1s, 2s -- bounded, not a spin loop
                continue
            raise HttpError(method, url, exc.code, body) from None

        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(attempt)
                continue
            raise Unavailable(f"{method} {url}: {exc}") from None

    raise Unavailable(f"{method} {url}: {last_error}")
