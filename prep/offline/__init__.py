"""Offline bounded context.

Server-side support for the offline companion app described in
docs/OFFLINE.md: the read-only snapshot endpoint that seeds the
client's IndexedDB (milestone M1) and, in later milestones, the sync
endpoint that replays queued offline reviews through the real
scheduler.
"""
