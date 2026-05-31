"""In-memory identity provider for tests.

`FakeProvider` lets a test pin "the user this request is" without
touching env vars, headers, or external services. Use via:

    from prep.auth.providers import set_provider
    from prep.auth.providers.fake import FakeProvider

    set_provider(FakeProvider(external_id="alice@example.com", email="alice@example.com"))
    # ... run requests against the TestClient ...
    set_provider(None)  # restore env-driven resolution
"""

from __future__ import annotations

from fastapi import Request

from prep.auth.port import IdentityProvider, ResolvedUser, SignInUrls


class FakeProvider(IdentityProvider):
    """Returns a pre-configured ResolvedUser on every resolve."""

    name = "fake"

    def __init__(
        self,
        external_id: str = "test@example.com",
        email: str | None = "test@example.com",
        display_name: str | None = "Test User",
        profile_pic_url: str | None = None,
        signed_in: bool = True,
    ) -> None:
        self._user = ResolvedUser(
            external_id=external_id,
            email=email,
            display_name=display_name,
            profile_pic_url=profile_pic_url,
            provider=self.name,
        )
        self._signed_in = signed_in

    def resolve(self, request: Request) -> ResolvedUser | None:
        return self._user if self._signed_in else None

    def urls(self) -> SignInUrls:
        return SignInUrls(
            sign_in="/fake/sign-in",
            sign_out="/fake/sign-out",
            account="/fake/account",
        )
