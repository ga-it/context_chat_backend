"""Backend package exports."""
from . import r2r_overrides  # noqa: F401 - ensure runtime patches apply
from .r2r import R2rBackend

__all__ = ["R2rBackend"]
