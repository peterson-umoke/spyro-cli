"""Native OS keychain access via keyring.

The PRD requires: "Uses native OS secure stores to handle sensitive passwords."

This module wraps the `keyring` library to provide:
- Storing one password per profile:user in the OS keychain
- Retrieving them when needed
- Graceful fallback to getpass prompts when keychain is unavailable
- ``SPYRO_PASSWORD_<PROFILE>`` / ``SPYRO_PASSWORD`` environment variables for
  headless hosts and CI, where no OS keychain exists
"""

from __future__ import annotations

import getpass
import logging
import os
import re
import sys
from typing import Optional

log = logging.getLogger("spyro.keychain")

# Keyring service name
SERVICE_NAME = "spyro-cli"


def _env_credential(profile: str) -> Optional[str]:
    """Password from ``SPYRO_PASSWORD_<PROFILE>`` (non-word chars -> ``_``) or ``SPYRO_PASSWORD``."""
    name = "SPYRO_PASSWORD_" + re.sub(r"\W", "_", profile).upper()
    return os.environ.get(name) or os.environ.get("SPYRO_PASSWORD") or None


def _keyring_available() -> bool:
    """Check if keyring is usable."""
    try:
        import keyring
        # Test if a backend is available
        backend = keyring.get_keyring()
        return backend is not None and not isinstance(
            backend, keyring.backends.fail.Keyring
        )
    except Exception:
        return False


def store_credential(
    profile: str,
    username: str,
    password: str,
) -> bool:
    """Store a credential in the OS keychain.

    One password per profile:user — used for both SSH and sudo.

    Args:
        profile: Profile name (e.g., "staging")
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

        key = f"{profile}:{username}"
        keyring.set_password(SERVICE_NAME, key, password)
        log.debug(f"Stored credential for {key}")
        return True
    except Exception as e:
        log.warning(f"Failed to store credential: {e}")
        return False


def get_credential(
    profile: str,
    username: str,
) -> Optional[str]:
    """Retrieve a credential from the OS keychain.

    Args:
        profile: Profile name
        username: Remote username

    Returns:
        The password if found, None otherwise.
    """
    env_pw = _env_credential(profile)
    if env_pw:
        return env_pw

    if not _keyring_available():
        return None

    try:
        import keyring

        key = f"{profile}:{username}"
        password = keyring.get_password(SERVICE_NAME, key)
        if password:
            log.debug(f"Retrieved credential for {key}")
        return password
    except Exception as e:
        log.warning(f"Failed to retrieve credential: {e}")
        return None


def delete_credential(
    profile: str,
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

        key = f"{profile}:{username}"
        keyring.delete_password(SERVICE_NAME, key)
        log.debug(f"Deleted credential for {key}")
        return True
    except Exception as e:
        log.warning(f"Failed to delete credential: {e}")
        return False


def prompt_for_credential(
    profile: str,
    username: str,
    *,
    store: bool = True,
) -> str:
    """Get a credential, checking keychain first, then prompting.

    If keychain is available and has the credential, returns it.
    Otherwise, prompts the user and optionally stores in keychain.

    Args:
        profile: Profile name
        username: Remote username
        store: Whether to store the prompted credential in keychain

    Returns:
        The password string.
    """
    # Try keychain first
    cached = get_credential(profile, username)
    if cached:
        return cached

    # Nobody to ask (CI, cron, pipes): don't hang or crash on getpass.
    if not sys.stdin.isatty():
        log.debug("No stored credential for %s@%s and stdin is not a tty", username, profile)
        return ""

    # Prompt user
    password = getpass.getpass(f"  password for {username}@{profile}: ")

    # Store in keychain if available
    if store and password:
        store_credential(profile, username, password)

    return password
