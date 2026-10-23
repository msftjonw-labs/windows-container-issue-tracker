import time
from datetime import timezone
from email.utils import parsedate_to_datetime

import requests


class GitHubClient:
    MAX_ATTEMPTS = 3
    MAX_WAIT_SECONDS = 900
    TRANSIENT_STATUS_CODES = {500, 502, 503, 504}

    def __init__(self, token):
        self.headers = {
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
        }
        self.waited = 0
        self.reset_at = {}
        self.last_mutation = None

    def wait(self, seconds):
        seconds = max(0, seconds)
        if self.waited + seconds > self.MAX_WAIT_SECONDS:
            raise RuntimeError("GitHub rate-limit wait budget exhausted; retry the workflow later.")
        self.waited += seconds
        time.sleep(seconds)

    @staticmethod
    def retry_after(headers):
        value = headers.get("Retry-After")
        if value is None:
            return None
        try:
            return max(0, float(value))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return max(0, parsed.timestamp() - time.time())
            except (TypeError, ValueError):
                return None

    def request(self, method, url, *, graphql=False, mutation=False, **kwargs):
        resource = "graphql" if graphql else "core"
        for attempt in range(self.MAX_ATTEMPTS):
            reset = self.reset_at.get(resource, 0)
            if reset > time.time():
                self.wait(reset - time.time() + 1)
                self.reset_at.pop(resource, None)
            if mutation and self.last_mutation is not None:
                delay = 1 - (time.monotonic() - self.last_mutation)
                if delay > 0:
                    time.sleep(delay)
            if mutation:
                self.last_mutation = time.monotonic()

            # A timed-out write may have been applied; do not replay it blindly.
            try:
                response = requests.request(
                    method, url, headers=self.headers, timeout=30, **kwargs
                )
            except (requests.ConnectionError, requests.Timeout):
                if mutation or attempt == self.MAX_ATTEMPTS - 1:
                    raise
                self.wait(2 ** attempt)
                continue
            response_headers = response.headers
            remaining = response_headers.get("X-RateLimit-Remaining")
            reserve = 50 if graphql else 10
            if remaining is not None and int(remaining) <= reserve:
                self.reset_at[resource] = float(response_headers.get("X-RateLimit-Reset", 0))
            payload = response.json() if response.status_code == 200 and graphql else None
            errors = payload.get("errors", []) if isinstance(payload, dict) else []
            error_text = str(errors).lower()
            rate_limited = (
                response.status_code == 429
                or (response.status_code == 403 and (
                    response_headers.get("X-RateLimit-Remaining") == "0"
                    or response_headers.get("Retry-After") is not None
                    or "rate limit" in response.text.lower()
                    or "secondary" in response.text.lower()
                ))
                or any(
                    error.get("type") == "RATE_LIMITED"
                    or "rate limit" in error.get("message", "").lower()
                    for error in errors
                )
            )
            transient = (
                response.status_code in self.TRANSIENT_STATUS_CODES
                or any(marker in error_text for marker in (
                    "something went wrong while executing your query", "timeout"
                ))
            )
            if rate_limited or transient:
                # GraphQL may return both data and errors; replaying a partial
                # mutation could duplicate content (notably digest creation).
                if mutation and graphql and payload and payload.get("data"):
                    raise RuntimeError("GitHub returned a partial mutation result; not replaying it.")
                if mutation and not graphql and transient and not rate_limited:
                    raise RuntimeError("GitHub write outcome unknown; not replaying it.")
                if attempt == self.MAX_ATTEMPTS - 1:
                    raise RuntimeError(f"GitHub API retry attempts exhausted ({response.status_code}): {errors or response.reason}")
                if rate_limited:
                    delay = self.retry_after(response_headers)
                    if delay is None:
                        delay = max(60 * (2 ** attempt), self.reset_at.get(resource, 0) - time.time() + 1)
                    delay = max(delay, self.reset_at.get(resource, 0) - time.time() + 1)
                    self.reset_at.pop(resource, None)
                    print(f"GitHub rate limited; waiting {delay:.0f}s before retry.")
                else:
                    delay = 2 ** attempt
                self.wait(delay)
                continue
            response.raise_for_status()
            if errors:
                raise RuntimeError(f"GraphQL Errors: {errors}")
            if graphql:
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
                    raise RuntimeError("GitHub returned no GraphQL data.")
                return payload["data"]
            return response
