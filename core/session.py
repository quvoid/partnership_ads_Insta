"""
================================================================================
INSTAGRAM SESSION LOADER
================================================================================
The one place credentials come from. Nothing else in core/ may hardcode them.

Cookies are read from a git-ignored .env file in the repo root:

    IG_SESSIONID=...
    IG_CSRFTOKEN=...
    IG_DS_USER_ID=...
    IG_MID=...

Get them from a logged-in browser: DevTools -> Application -> Cookies ->
instagram.com. A sessionid IS a login - anyone holding it is logged in as that
account with no password or 2FA. Never paste it into a source file, a doc, or
a commit. If one leaks, log the account out of all sessions in Instagram's
security settings; that invalidates it regardless of where it was copied.
================================================================================
"""

import os
from typing import Dict, List

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(BASE_DIR, ".env")

_REQUIRED = {
    "sessionid": "IG_SESSIONID",
    "csrftoken": "IG_CSRFTOKEN",
    "ds_user_id": "IG_DS_USER_ID",
}
_OPTIONAL = {
    "mid": "IG_MID",
}


def _read_env_file(path: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_cookies() -> Dict[str, str]:
    """
    Returns the Instagram cookie dict. Real environment variables win over the
    .env file, so CI or a shell export can override it.
    """
    file_vals = _read_env_file(ENV_FILE)

    def get(var: str) -> str:
        return os.environ.get(var) or file_vals.get(var, "")

    cookies: Dict[str, str] = {}
    missing: List[str] = []
    for cookie_name, var in _REQUIRED.items():
        val = get(var)
        if val:
            cookies[cookie_name] = val
        else:
            missing.append(var)
    for cookie_name, var in _OPTIONAL.items():
        val = get(var)
        if val:
            cookies[cookie_name] = val

    if missing:
        raise RuntimeError(
            "Instagram session not configured. Missing: " + ", ".join(missing) + "\n"
            f"Create {ENV_FILE} with:\n"
            "    IG_SESSIONID=...\n    IG_CSRFTOKEN=...\n    IG_DS_USER_ID=...\n    IG_MID=...\n"
            "(copy them from a logged-in browser: DevTools -> Application -> Cookies -> instagram.com)"
        )
    return cookies


def playwright_cookies(cookies: Dict[str, str]) -> List[Dict[str, str]]:
    return [{"name": k, "value": v, "domain": ".instagram.com", "path": "/"}
            for k, v in cookies.items()]


def describe(cookies: Dict[str, str]) -> str:
    """Safe to print: names and the account id, never the secret values."""
    return (f"session for ds_user_id={cookies.get('ds_user_id', '?')} "
            f"(cookies present: {', '.join(sorted(cookies))})")
