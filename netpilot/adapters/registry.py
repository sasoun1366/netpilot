"""Vendor adapter registry.

Adding a vendor means writing one module next to this one and adding it to ``_ADAPTERS``.
The registry is what makes the rest of netpilot vendor-agnostic: the dashboard, the deploy
engine and both UIs only ever ask for ``get_adapter(device.vendor)``.
"""

from __future__ import annotations

from typing import Type

from .base import Adapter, AdapterError, CommandResult, SessionInfo
from .cisco import CiscoAdapter
from .generic import GenericSSHAdapter
from .mikrotik import MikroTikAdapter

_ADAPTERS: dict[str, Type[Adapter]] = {
    MikroTikAdapter.name: MikroTikAdapter,
    CiscoAdapter.name: CiscoAdapter,
    GenericSSHAdapter.name: GenericSSHAdapter,
}

#: Aliases so operators can type what they actually call the box.
_ALIASES: dict[str, str] = {
    "routeros": "mikrotik",
    "ros": "mikrotik",
    "mikrotik routeros": "mikrotik",
    "ios": "cisco",
    "ios-xe": "cisco",
    "iosxe": "cisco",
    "cisco ios": "cisco",
    "switch": "cisco",
    "ssh": "generic",
    "linux": "generic",
    "generic ssh": "generic",
}


def register(cls: Type[Adapter]) -> Type[Adapter]:
    """Register an adapter class (also usable as a decorator)."""
    _ADAPTERS[cls.name] = cls
    return cls


def available_vendors() -> list[dict[str, object]]:
    """Vendor metadata for the UI's dropdowns."""
    return [
        {
            "name": cls.name,
            "label": cls.label,
            "supports_deploy": cls.supports_deploy,
            "rollback_support": getattr(cls, "rollback_support", "manual"),
        }
        for cls in sorted(_ADAPTERS.values(), key=lambda c: c.label)
    ]


def resolve_vendor(vendor: str) -> str:
    """Normalise a vendor string to a registry key (``""`` when unknown)."""
    if not vendor:
        return ""
    key = vendor.strip().lower()
    if key in _ADAPTERS:
        return key
    return _ALIASES.get(key, "")


def get_adapter(vendor: str) -> Type[Adapter]:
    """Return the adapter class for *vendor*, falling back to the generic shell."""
    key = resolve_vendor(vendor)
    if not key:
        raise AdapterError(
            f"unsupported vendor {vendor!r} — known vendors: {', '.join(sorted(_ADAPTERS))}"
        )
    return _ADAPTERS[key]


def supports_deploy(vendor: str) -> bool:
    try:
        return bool(get_adapter(vendor).supports_deploy)
    except AdapterError:
        return False


__all__ = [
    "Adapter",
    "AdapterError",
    "CommandResult",
    "SessionInfo",
    "register",
    "get_adapter",
    "resolve_vendor",
    "available_vendors",
    "supports_deploy",
]
