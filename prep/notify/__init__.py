"""prep.notify — Web Push notifications.

Public surface (used by app.py + routes):
- public_key_b64()  → VAPID server public key (browser subscribes against it)
- subscribe(user_id, sub) → store/refresh a browser push subscription
- send_to_user(user_id, title, body, url=None) → push to all the
  user's registered devices; prunes 404/410 subscriptions
- start_scheduler() → launch the background digest/when-ready loop
- VAPID_SUB → IANA "sub" claim sent on every push

Implementation is split between `push.py` (VAPID + fanout, the I/O
side) and `scheduler.py` (the periodic policy loop).
"""

from prep.notify.push import VAPID_SUB, public_key_b64, send_to_user, subscribe
from prep.notify.scheduler import start_scheduler

__all__ = [
    "VAPID_SUB",
    "public_key_b64",
    "send_to_user",
    "start_scheduler",
    "subscribe",
]
