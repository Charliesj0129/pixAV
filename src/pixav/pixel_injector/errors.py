"""Portable diagnostics shared by native control and storage adapters."""


class CanaryBlockedError(ValueError):
    """Safe, fixed diagnostic suitable for public output."""
