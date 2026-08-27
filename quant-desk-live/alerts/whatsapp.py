"""WhatsApp alerts via CallMeBot (free, personal-use, no account needed beyond
the one-time WhatsApp opt-in — see .env.example for setup).

Swap `_send_via_callmebot` for a Twilio call later if you outgrow it; nothing
else in the app needs to change since callers only ever use `maybe_alert()`.
"""
from __future__ import annotations

import logging
import time
from urllib.parse import quote

import requests

from config import config
from state import state

log = logging.getLogger("whatsapp")

# symbol+reason -> last time we alerted (epoch seconds), so we don't spam
# every tick the moment a threshold is crossed.
_last_sent: dict[str, float] = {}


def _send_via_callmebot(message: str) -> bool:
    if not config.whatsapp_configured:
        log.debug("WhatsApp not configured (missing CALLMEBOT_API_KEY / WHATSAPP_PHONE); skipping alert: %s", message)
        return False
    url = (
        "https://api.callmebot.com/whatsapp.php"
        f"?phone={config.WHATSAPP_PHONE}"
        f"&text={quote(message)}"
        f"&apikey={config.CALLMEBOT_API_KEY}"
    )
    try:
        resp = requests.get(url, timeout=10)
        ok = resp.status_code == 200
        if not ok:
            log.warning("CallMeBot returned %s: %s", resp.status_code, resp.text[:200])
        return ok
    except requests.RequestException as exc:
        log.warning("CallMeBot request failed: %s", exc)
        return False


def maybe_alert(key: str, message: str) -> bool:
    """Send `message` over WhatsApp unless we already alerted on this exact
    `key` (e.g. "NVDA:rsi_overbought") within the configured cooldown window.
    Returns True if a message was actually sent.
    """
    now = time.time()
    cooldown = config.ALERT_COOLDOWN_MINUTES * 60
    last = _last_sent.get(key, 0)
    if now - last < cooldown:
        return False
    if not config.whatsapp_configured:
        _last_sent[key] = now
        state.log_alert(message + "  (WhatsApp not configured — logged here only)")
        log.debug("WhatsApp not configured; logged only: %s", message)
        return False

    sent = _send_via_callmebot(message)
    _last_sent[key] = now  # cool down even on failure, so a flapping symbol can't hammer the API
    state.log_alert(message + ("" if sent else "  (send failed — check CALLMEBOT_API_KEY/WHATSAPP_PHONE)"))
    if sent:
        log.info("WhatsApp alert sent: %s", message)
    return sent
