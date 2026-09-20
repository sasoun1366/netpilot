"""Vendor adapters: one module per device family, one shared registry."""

from .base import Adapter, AdapterError, CommandResult, SessionInfo
from .cisco import CiscoAdapter
from .generic import GenericSSHAdapter
from .mikrotik import MikroTikAdapter
from .registry import available_vendors, get_adapter, register, resolve_vendor, supports_deploy
from .ssh import SSHAdapter, clean_terminal, collapse_backspaces, interactive_probe

__all__ = [
    "Adapter",
    "AdapterError",
    "CommandResult",
    "SessionInfo",
    "SSHAdapter",
    "MikroTikAdapter",
    "CiscoAdapter",
    "GenericSSHAdapter",
    "register",
    "get_adapter",
    "resolve_vendor",
    "available_vendors",
    "supports_deploy",
    "interactive_probe",
    "clean_terminal",
    "collapse_backspaces",
]
