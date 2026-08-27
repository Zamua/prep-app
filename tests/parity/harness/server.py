"""The parity target: a base URL, seeded through `POST /_parity/seed`.

`PARITY_BASE_URL` names it. A local run points at a celld node started
by `worker/scripts/run-node.sh`; a deployed run points at the fleet.
"""

from __future__ import annotations

import time

import httpx

from tests.parity.harness.constants import (
    PARITY_USER,
    internal_token,
)

BASE_URL_ENV = "PARITY_BASE_URL"


SEED_ATTEMPTS = 4


def seed(base_url: str, user: str, profile: str, *, token: str | None = None) -> dict:
    """Re-seed the target, retrying a 5xx.

    A cell-backed target answers the wipe from a replicated store: when a
    durability proof times out the cell restarts and the write never landed,
    which the runtime documents as retryable. A 4xx is the request's own
    fault and is raised on the first answer.
    """
    token = internal_token(token)
    for attempt in range(1, SEED_ATTEMPTS + 1):
        r = httpx.post(
            f"{base_url}/_parity/seed",
            json={"user": user, "profile": profile},
            headers={"X-Internal-Token": token},
            timeout=60.0,
        )
        if r.status_code == 200:
            return r.json()
        if r.status_code < 500 or attempt == SEED_ATTEMPTS:
            raise RuntimeError(f"seed {profile!r} for {user!r}: {r.status_code} {r.text[:300]}")
        time.sleep(attempt)
    raise AssertionError("unreachable")


class RemoteJobs:
    """The `jobs` handle of the target.

    A flow reaches past the app for one screen only: the job whose record is
    deleted mid-flight, which no route can ask for. The target's own parity
    route abandons it.
    """

    def __init__(self, target: ParityTarget):
        self._target = target

    def abandon_workflow(self, workflow_id: str, reason: str = "parity") -> None:
        r = httpx.post(
            f"{self._target.base_url}/_parity/job/abandon",
            json={"id": workflow_id, "owner": PARITY_USER},
            headers={"X-Internal-Token": internal_token(self._target.token)},
            timeout=60.0,
        )
        if r.status_code != 200:
            raise RuntimeError(f"abandon {workflow_id}: {r.status_code} {r.text[:300]}")


class ParityTarget:
    """A base URL plus the seed call."""

    def __init__(self, base_url: str, *, token: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.jobs: object | None = RemoteJobs(self)

    def seed(self, user: str, profile: str) -> dict:
        return seed(self.base_url, user, profile, token=self.token)
