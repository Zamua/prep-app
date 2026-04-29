"""prep.auth — identity + ownership.

Public surface:
- current_user(request) → dict (FastAPI Depends target)

The auth bounded context owns:
- identity: Tailscale header parsing → user dict (identity.py)
- editor settings: per-user editor_input_mode preference (repo.py)
- ownership: route-level guards (planned; will land alongside the
  first repo write that needs cross-resource ownership beyond the
  user_id WHERE-clause discipline already in db.py)
"""

from prep.auth.identity import current_user

__all__ = ["current_user"]
