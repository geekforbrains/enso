"""Resolve opt-in secret references without projecting values into config or env."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from collections.abc import Mapping

ONEPASSWORD_HELPER = "~/.enso/lib/1password.sh"
ONEPASSWORD_TIMEOUT_SECONDS = 30


class SecretResolutionError(RuntimeError):
    """A configured secret reference could not be read or updated safely."""


def _config_secret_reference(
    config: Mapping[str, object], key: str,
) -> tuple[str, str] | None:
    """Return a validated 1Password item/field pair when one is configured."""
    reference_key = f"{key}_1password"
    if reference_key not in config:
        return None

    reference = config[reference_key]
    if not isinstance(reference, Mapping):
        raise SecretResolutionError(
            f"{reference_key} must contain 1Password item and field names"
        )
    item = reference.get("item")
    field = reference.get("field")
    if not isinstance(item, str) or not item.strip():
        raise SecretResolutionError(f"{reference_key}.item must be a non-empty string")
    if not isinstance(field, str) or not field.strip():
        raise SecretResolutionError(f"{reference_key}.field must be a non-empty string")
    return item, field


def _helper_path(helper_path: str | None) -> str:
    """Return the expanded helper path or fail with a safe diagnostic."""
    helper = os.path.expanduser(helper_path or ONEPASSWORD_HELPER)
    if not os.path.isfile(helper):
        raise SecretResolutionError(f"1Password helper not found: {helper}")
    return helper


def _run_helper(
    shell_command: str,
    helper: str,
    item: str,
    field: str,
    *,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a helper call in a process group that can be fully timed out."""
    argv = [
        "/bin/bash",
        "-c",
        shell_command,
        "enso-secret",
        helper,
        item,
        field,
    ]
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(
            input=input_text,
            timeout=ONEPASSWORD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise
    return subprocess.CompletedProcess(
        argv,
        process.returncode,
        stdout,
        stderr,
    )


def resolve_config_secret(
    config: Mapping[str, object],
    key: str,
    *,
    helper_path: str | None = None,
) -> str:
    """Return ``key`` from a 1Password reference or its legacy literal value.

    A reference is configured as ``<key>_1password`` with non-empty ``item``
    and ``field`` strings. When present it takes precedence over the literal
    key, so migrations cannot accidentally keep using a stale plaintext copy.
    """
    reference = _config_secret_reference(config, key)
    if reference is None:
        if key not in config:
            return ""
        value = config[key]
        if not isinstance(value, str):
            raise SecretResolutionError(f"{key} must be a string")
        return value

    item, field = reference
    helper = _helper_path(helper_path)

    try:
        result = _run_helper(
            'source "$1"; op_secret "$2" "$3"',
            helper,
            item,
            field,
        )
    except subprocess.TimeoutExpired:
        raise SecretResolutionError(
            f"Timed out resolving {key} from 1Password"
        ) from None

    if result.returncode != 0:
        raise SecretResolutionError(
            f"Could not resolve {key} from 1Password (helper exit {result.returncode})"
        )
    value = result.stdout.strip()
    if not value:
        raise SecretResolutionError(f"1Password returned an empty value for {key}")
    return value


def update_config_secret_reference(
    config: Mapping[str, object],
    key: str,
    value: str,
    *,
    helper_path: str | None = None,
) -> bool:
    """Update a configured 1Password field, returning false for a literal key.

    The value is supplied to the helper shell over stdin. It never appears in
    the child process argv, command string, captured diagnostics, or config.
    """
    reference = _config_secret_reference(config, key)
    if reference is None:
        return False
    if not isinstance(value, str) or not value:
        raise SecretResolutionError(f"Refusing to write an empty value for {key}")

    item, field = reference
    helper = _helper_path(helper_path)
    try:
        result = _run_helper(
            'source "$1"; IFS= read -r secret; op_set_secret "$2" "$3" "$secret"',
            helper,
            item,
            field,
            input_text=f"{value}\n",
        )
    except subprocess.TimeoutExpired:
        raise SecretResolutionError(
            f"Timed out updating {key} in 1Password"
        ) from None

    if result.returncode != 0:
        raise SecretResolutionError(
            f"Could not update {key} in 1Password (helper exit {result.returncode})"
        )
    return True
