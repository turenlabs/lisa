from lisa import net

ENDPOINT = "https://api.typesafe.ai/v1/systemone"


class TypeSafeAuthError(RuntimeError):
    pass


def system_one(api_key: str, body: dict) -> dict:
    """Calls the System One endpoint (with retries) and returns the parsed response."""
    try:
        response = net.send("POST", ENDPOINT, {"Authorization": f"Bearer {api_key}"}, body)
    except OSError as error:
        raise RuntimeError(f"Could not reach TypeSafe: {error}") from error
    if response.status == 401:
        raise TypeSafeAuthError("TypeSafe rejected the API key (401). Check the `api-key` input.")
    if not response.ok:
        raise RuntimeError(f"TypeSafe request failed ({response.status}): {response.text()[:500]}")
    return response.json()
