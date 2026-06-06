"""Native OS keychain access via keyring.

The PRD requires: "Uses native OS secure stores to handle sensitive passwords."

This module wraps the `keyring` library to provide:
- Storing SSH/sudo passwords in the OS keychain
- Retrieving them when needed
- Graceful fallback to getpass prompts when keychain is unavailable
"""

from __future__ import annotations

import getpass
import logging
from typing import Optional

log = logging.getLogger("spyro.keychain")

# Keyring service name
SERVICE_NAME = "spyro-cli"


def _keyring_available() -> bool:
    """Check if keyring is usable."""
    try:
        import keyring
        # Test if a backend is available
        backend = keyring.get_keyring()
        return backend is not None and not isinstance(
            backend, keyring.fail.Keyring
        )
    except Exception:
        return False


def store_credential(
    profile: str,
    credential_type: str,
    username: str,
    password: str,
) -> bool:
    """Store a credential in the OS keychain.

    Args:
        profile: Profile name (e.g., "staging")
        credential_type: "ssh" or "sudo"
        username: Remote username
        password: The password to store

    Returns:
        True if stored successfully, False otherwise.
    """
    if not _keyring_available():
        log.debug("Keyring not available, skipping store")
        return False

    try:
        import keyring

        key = f"{profile}:{credential_type}:{username}"
        keyring.set_password(SERVICE_NAME, key, password)
        log.debug(f"Stored credential for {key}")
        return True
    except Exception as e:
        log.warning(f"Failed to store credential: {e}")
        return False


def get_credential(
    profile: str,
    credential_type: str,
    username: str,
) -> Optional[str]:
    """Retrieve a credential from the OS keychain.

    Args:
        profile: Profile name
        credential_type: "ssh" or "sudo"
        username: Remote username

    Returns:
        The password if found, None otherwise.
    """
    if not _keyring_available():
        return None

    try:
        import keyring

        key = f"{profile}:{credential_type}:{username}"
        password = keyring.get_password(SERVICE_NAME, key)
        if password:
            log.debug(f"Retrieved credential for {key}")
        return password
    except Exception as e:
        log.warning(f"Failed to retrieve credential: {e}")
        return None


def delete_credential(
    profile: str,
    credential_type: str,
    username: str,
) -> bool:
    """Delete a credential from the OS keychain.

    Returns:
        True if deleted successfully, False otherwise.
    """
    if not _keyring_available():
        return False

    try:
        import keyring

        key = f"{profile}:{credential_type}:{username}"
        keyring.delete_password(SERVICE_NAME, key)
        log.debug(f"Deleted credential for {key}")
        return True
    except Exception as e:
        log.warning(f"Failed to delete credential: {e}")
        return False


def prompt_for_credential(
    profile: str,
    credential_type: str,
    username: str,
    *,
    store: bool = True,
) -> str:
    """Get a credential, checking keychain first, then prompting.

    If keychain is available and has the credential, returns it.
    Otherwise, prompts the user and optionally stores in keychain.

    Args:
        profile: Profile name
        credential_type: "ssh" or "sudo"
        username: Remote username
        store: Whether to store the prompted credential in keychain

    Returns:
        The password string.
    """
    # Try keychain first
    cached = get_credential(profile, credential_type, username)
    if cached:
        return cached

    # Prompt user
    label = f"{credential_type} password for {username}@{profile}"
    password = getpass.getpass(f"{label}: ")

    # Store in keychain if available
    if store and password:
        store_credential(profile, credential_type, username, password)

    return password
