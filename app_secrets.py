"""Shared credential utility for Hive Mind.

Reads secrets from environment variables (typically loaded from the repo's
``.env`` at process start). The previous keyring layer was a plaintext-file
backend dressed up as a keyring — same threat model as ``.env`` with extra
indirection — and was retired in Phase B6. Replace this with a real secret
store (system keyring, vault, KMS) when one is available; the public
``get_credential`` signature stays so callers won't change shape.
"""

import os


def get_credential(key: str) -> str | None:
    """Get a secret value by key.

    Args:
        key: Env-var name (e.g. ``"COINGECKO_API_KEY"``).

    Returns:
        The credential value, or ``None`` if the env var is unset.
    """
    return os.getenv(key)
