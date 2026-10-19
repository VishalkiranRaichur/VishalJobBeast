from __future__ import annotations

from .generic import GenericProvider


class LeverProvider(GenericProvider):
    name = "lever"
    allow_submit = True
