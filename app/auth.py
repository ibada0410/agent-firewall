"""Reviewer sign-in: one demo reviewer account from environment variables,
kept in an HMAC-signed, HTTP-only cookie."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, Response

from .config import Settings

COOKIE = "af_reviewer"
TTL = 12 * 3600
log = logging.getLogger("agent_firewall")


class Auth:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.user = settings.reviewer_user
        self.password = settings.reviewer_password
        self.generated = False
        if not self.password:
            self.password = secrets.token_urlsafe(9)
            self.generated = True
            log.warning("REVIEWER_PASSWORD is not set. Generated a one-time reviewer password for this run: %s",
                        self.password)
        self._key = settings.session_secret.encode()
        self._attempts: dict[str, deque[float]] = defaultdict(deque)

    def _sign(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()

    def issue(self, response: Response, user: str) -> None:
        payload = f"{user}|{int(time.time()) + TTL}"
        token = base64.urlsafe_b64encode(payload.encode()).decode() + "." + self._sign(payload)
        response.set_cookie(COOKIE, token, max_age=TTL, httponly=True, samesite="strict",
                            secure=self.settings.secure_cookies)

    def clear(self, response: Response) -> None:
        response.delete_cookie(COOKIE)

    def current(self, request: Request) -> str | None:
        token = request.cookies.get(COOKIE)
        if not token or "." not in token:
            return None
        b64, sig = token.rsplit(".", 1)
        try:
            payload = base64.urlsafe_b64decode(b64.encode()).decode()
        except (ValueError, UnicodeDecodeError):
            return None
        if not hmac.compare_digest(self._sign(payload), sig):
            return None
        user, _, exp = payload.partition("|")
        if not exp.isdigit() or int(exp) < time.time():
            return None
        return user

    def require(self, request: Request) -> str:
        user = self.current(request)
        if not user:
            raise HTTPException(401, "Sign in as a reviewer to do this.")
        return user

    def check_login(self, ip: str, user: str, password: str) -> bool:
        window = self._attempts[ip]
        now = time.time()
        while window and now - window[0] > 300:
            window.popleft()
        if len(window) >= 8:
            raise HTTPException(429, "Too many sign-in attempts. Wait five minutes and try again.")
        ok = hmac.compare_digest(user, self.user) & hmac.compare_digest(password, self.password or "")
        if not ok:
            window.append(now)
        return ok
