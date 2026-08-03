"""HTTP Basic auth for the whole dashboard - the .htaccess equivalent.

There is no Apache in front of this process, so the password prompt has to come
from the app itself. A middleware rather than a per-route dependency, because
the point is that NOTHING is readable without a login: adding a page later must
not be able to leave a hole, and the /static mount takes no dependencies at all.

The agent push endpoints are exempt on purpose. agent.py already authenticates
with the x-agent-secret header, it runs unattended on every monitored server,
and it would have to be redeployed everywhere the day this password changes.
"""
import base64
import secrets
from typing import Iterable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

# Machine-to-machine POSTs, already guarded by the shared secret.
AGENT_PATHS = (
    "/api/pm2/report",
    "/api/cron/report",
    "/api/partners/feed-count",
)


def _unauthorized(detail: str = "Not authenticated") -> Response:
    # WWW-Authenticate is what makes the browser show its login box - without
    # it a 401 is just a blank error page.
    return JSONResponse(
        status_code=401,
        content={"detail": detail},
        headers={"WWW-Authenticate": 'Basic realm="Ops Dashboard"'},
    )


class BasicAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, username: str, password: str, exempt: Iterable[str] = AGENT_PATHS):
        super().__init__(app)
        self.username = username
        self.password = password
        self.exempt = tuple(exempt)

    async def dispatch(self, request, call_next):
        if request.url.path in self.exempt:
            return await call_next(request)

        header = request.headers.get("authorization", "")
        scheme, _, encoded = header.partition(" ")
        if scheme.lower() != "basic" or not encoded:
            return _unauthorized()

        try:
            user, _, pw = base64.b64decode(encoded).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return _unauthorized("Invalid credentials")

        # compare_digest on both, and both compared before deciding, so the
        # response time says nothing about how much of the guess was right.
        ok_user = secrets.compare_digest(user, self.username)
        ok_pw = secrets.compare_digest(pw, self.password)
        if not (ok_user and ok_pw):
            return _unauthorized("Invalid credentials")

        return await call_next(request)
