"""Client for the separate Chest Bot API."""

import asyncio
import json
import os
import urllib.error
import urllib.request
import uuid


class ChestAPIError(RuntimeError):
    """Raised when the Chest Bot API request fails."""


async def give_chests(
    user_id: int,
    amount: int,
    chest_type: str = "chest",
    transaction_id: str | None = None,
) -> dict:
    url = os.environ.get("CHEST_API_URL", "").strip()
    key = os.environ.get("CHEST_API_KEY", "").strip()

    if not url:
        raise ChestAPIError("CHEST_API_URL is not configured.")

    if not key:
        raise ChestAPIError("CHEST_API_KEY is not configured.")

    if int(user_id) <= 0 or not 1 <= int(amount) <= 10000:
        raise ChestAPIError("Invalid user ID or amount.")

    if chest_type not in ("chest", "ultra"):
        raise ChestAPIError("Unsupported chest type.")

    payload = {
        "user_id": int(user_id),
        "amount": int(amount),
        "chest_type": chest_type,
        "transaction_id": transaction_id or str(uuid.uuid4()),
    }

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )

    def send():
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return (
                    response.status,
                    response.read().decode("utf-8", errors="replace"),
                )
        except urllib.error.HTTPError as exc:
            return (
                exc.code,
                exc.read().decode("utf-8", errors="replace"),
            )
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ChestAPIError(
                f"Could not reach Chest API: {exc}"
            ) from exc

    status, raw = await asyncio.to_thread(send)

    try:
        result = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise ChestAPIError(
            f"Chest API returned invalid JSON (HTTP {status})."
        ) from exc

    if status != 200 or not result.get("ok"):
        raise ChestAPIError(
            str(result.get("error", f"HTTP {status}"))
        )

    return result


async def setup(bot):
    print("Chest API client loaded.")
