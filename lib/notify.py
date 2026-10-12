"""Optional SeaTalk notifications. Two independent channels, both optional:

1) GROUP webhook — config.SEATALK_WEBHOOK
   The ONLY way to post into a group chat. Needs a "System Account"
   (create on the SeaTalk Open Platform portal, open.seatalk.io, desktop;
   in old SeaTalk it lived under group settings). Webhook looks like
   https://openapi.seatalk.io/webhook/group/xxxxxxxx
   Payload per SeaTalk docs: {"tag": "text", "text": {"content": ...}}

2) APP-BOT DM — config.SEATALK_APP_ID / SEATALK_APP_SECRET / SEATALK_NOTIFY_EMAIL
   Uses an existing Open Platform bot app (e.g. "OB SOCPACKED BOT") to DM the
   summary to one user. NOTE: SeaTalk's own SDK leaves group-send as
   NotImplementedError for app bots — they can ONLY single-chat, so this is a
   DM, not a group post. The target user must have messaged the bot once
   (become a subscriber) before DMs can be delivered.

Either/both may be configured; empty keys = that channel disabled and the run
continues silently. Notifications NEVER raise — a chat failure must not kill
a run. Limits: text ≤ 4096 bytes; webhook 60 msg/min.
"""

from __future__ import annotations

import json
import urllib.request

from lib import cli

_HOST = "https://openapi.seatalk.io"


def _config():
    try:
        import config
        return config
    except Exception:
        return None


def _post(path: str, payload: dict, token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        _HOST + path, data=json.dumps(payload).encode("utf-8"),
        headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


# ------------------------------------------------------------- group webhook

def _send_webhook(url: str, message: str) -> bool:
    try:
        _post_url(url, {"tag": "text", "text": {"content": message[:4000]}})
        cli.ok("SeaTalk group notified (webhook).")
        return True
    except Exception as e:
        cli.warn("SeaTalk webhook failed (run continues): " + str(e).splitlines()[0])
        return False


def _post_url(url: str, payload: dict) -> None:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()


# ------------------------------------------------------------- app-bot DM

def _send_bot_dm(app_id: str, app_secret: str, email: str, message: str) -> bool:
    """DM `email` from the Open Platform bot app (app_id/app_secret)."""
    try:
        tok = _post("/auth/app_access_token",
                    {"app_id": app_id, "app_secret": app_secret})
        token = tok.get("app_access_token")
        if not token:
            raise ValueError(f"no app_access_token in response: {tok}")
        emp = _post("/contacts/v2/get_employee_code_with_email",
                    {"emails": [email]}, token)
        code = None
        for e in emp.get("employees", []):
            if e.get("code", -1) == 0 and e.get("employee_status", 0) == 2:
                code = e.get("employee_code")
        if not code:
            raise ValueError(f"no employee_code for {email} "
                             "(check the email / bot data scope)")
        _post("/messaging/v2/single_chat",
              {"employee_code": code,
               "message": {"tag": "text",
                           "text": {"content": message[:4000]}}},
              token)
        cli.ok("SeaTalk DM sent via app bot.")
        return True
    except Exception as e:
        cli.warn("SeaTalk bot DM failed (run continues): " + str(e).splitlines()[0])
        return False


# ------------------------------------------------------------- entry point

def send_seatalk(message: str) -> bool:
    """Send one text message over every configured SeaTalk channel.
    Returns True if at least one channel delivered it."""
    cfg = _config()
    if cfg is None or not (message or "").strip():
        return False
    sent = False

    webhook = (getattr(cfg, "SEATALK_WEBHOOK", "") or "").strip()
    if webhook:
        sent |= _send_webhook(webhook, message)

    app_id = (getattr(cfg, "SEATALK_APP_ID", "") or "").strip()
    app_secret = (getattr(cfg, "SEATALK_APP_SECRET", "") or "").strip()
    email = (getattr(cfg, "SEATALK_NOTIFY_EMAIL", "") or "").strip()
    if app_id and app_secret and email:
        sent |= _send_bot_dm(app_id, app_secret, email, message)

    return sent
