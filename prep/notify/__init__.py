"""prep.notify — Web Push notifications.

Public surface (used by app.py + routes):
- public_key_b64()  → VAPID server public key (browser subscribes against it)
- subscribe(user_id, sub) → store/refresh a browser push subscription
- send_to_user(user_id, title, body, url=None) → push to all the
  user's registered devices; prunes 404/410 subscriptions
- start_scheduler() → launch the background digest/when-ready loop
- VAPID_SUB → IANA "sub" claim sent on every push

The implementation lives in `_legacy_module.py` for now — phase 7
landed the bounded-context skeleton (entities/repo/routes) and a
later refactor splits the implementation into push.py + scheduler.py.
"""

from prep.notify._legacy_module import (
    VAPID_SUB,
    public_key_b64,
    send_to_user,
    start_scheduler,
    subscribe,
)

__all__ = [
    "VAPID_SUB",
    "public_key_b64",
    "send_to_user",
    "start_scheduler",
    "subscribe",
]
