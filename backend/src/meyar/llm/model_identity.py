"""Restricted local Ollama identities used at every production trust boundary."""

import re

LOCAL_MODEL = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}:[a-z0-9][a-z0-9._-]{0,99}$")


def is_local_model_identity(name: str) -> bool:
    if not LOCAL_MODEL.fullmatch(name):
        return False
    _base, tag = name.split(":", 1)
    return tag != "cloud" and not tag.endswith("-cloud")
