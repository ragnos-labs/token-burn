# SPDX-License-Identifier: Apache-2.0
"""The released, bounded coding profile. Loading it changes no host settings."""

from __future__ import annotations

import hashlib
from importlib.resources import files

from token_burn import __version__
from token_burn._local import ContractError


def show() -> dict[str, str]:
    data = files("token_burn").joinpath("profiles/coding-efficiency.md").read_bytes()
    if len(data) > 4096:
        raise ContractError("profile_too_large")
    return {
        "schema_version": "token_burn.profile.v1",
        "profile_id": "coding-efficiency",
        "profile_version": "0.0.11",
        "runtime_version": __version__,
        "document": data.decode("utf-8"),
        "document_sha256": hashlib.sha256(data).hexdigest(),
    }
