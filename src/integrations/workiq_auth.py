import asyncio
from threading import Lock
from uuid import UUID

from msal import PublicClientApplication

from config.settings import Settings

WORKIQ_URL = "https://workiq.svc.cloud.microsoft/mcp"
WORKIQ_SCOPES = [
    "api://workiq.svc.cloud.microsoft/WorkIQ.Read",
    "api://workiq.svc.cloud.microsoft/WorkIQ.Act",
    "api://workiq.svc.cloud.microsoft/WorkIQAgent.Ask",
]


class WorkIQAuth:
    def __init__(self, tenant_id: str, client_id: str):
        try:
            tenant = str(UUID(tenant_id))
            client = str(UUID(client_id))
        except ValueError as exc:
            raise ValueError("Microsoft 365 tenant and client IDs must be valid UUIDs") from exc
        self.app = PublicClientApplication(
            client_id=client, authority=f"https://login.microsoftonline.com/{tenant}"
        )
        self.sign_in_lock = Lock()

    def sign_in(self) -> None:
        if not self.sign_in_lock.acquire(blocking=False):
            raise ValueError("Microsoft 365 sign-in is already in progress")
        try:
            result = self.app.acquire_token_interactive(scopes=WORKIQ_SCOPES, timeout=300)
            if not result.get("access_token"):
                raise ValueError(f"Microsoft 365 sign-in failed: {result.get('error', 'unknown error')}")
        finally:
            self.sign_in_lock.release()

    def token(self) -> str:
        accounts = self.app.get_accounts()
        if len(accounts) != 1:
            raise ValueError("Sign in to Microsoft 365 in this process first")
        result = self.app.acquire_token_silent(WORKIQ_SCOPES, account=accounts[0])
        if not result or not result.get("access_token"):
            raise ValueError("Microsoft 365 sign-in expired or requires interaction; sign in again")
        return result["access_token"]


_auth: WorkIQAuth | None = None
_auth_identity: tuple[str, str] | None = None


def get_workiq_auth() -> WorkIQAuth:
    global _auth, _auth_identity
    settings = Settings()
    if not settings.tenant_id or not settings.client_id:
        raise ValueError("Set TENANT_ID and CLIENT_ID in the environment")
    identity = (settings.tenant_id, settings.client_id)
    if _auth is None or _auth_identity != identity:
        _auth = WorkIQAuth(*identity)
        _auth_identity = identity
    return _auth


async def sign_in_cli() -> None:
    auth = await asyncio.to_thread(get_workiq_auth)
    print("Complete Microsoft 365 sign-in in your browser.", flush=True)
    await asyncio.to_thread(auth.sign_in)
    print("Microsoft 365 sign-in succeeded.", flush=True)
