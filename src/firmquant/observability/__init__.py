"""Operational health, reporting, logging, and alerting boundaries."""

from .health import CheckResult, Doctor

__all__ = ("CheckResult", "Doctor")
