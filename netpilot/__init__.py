"""netpilot — network monitoring dashboard and bulk configuration deployment."""

from __future__ import annotations

__version__ = "0.1.1"

from .models import (
    CheckConfig,
    CheckResult,
    Credential,
    Device,
    DeviceState,
    Event,
    Job,
    JobTarget,
    Template,
)

__all__ = [
    "__version__",
    "Device",
    "Credential",
    "CheckConfig",
    "CheckResult",
    "DeviceState",
    "Event",
    "Template",
    "Job",
    "JobTarget",
]
