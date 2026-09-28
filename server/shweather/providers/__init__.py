"""Remote data providers. Each module fetches from one service and normalises the result
into canonical units (see shweather.series and docs/ARCHITECTURE.md)."""


class ProviderError(RuntimeError):
    """A provider could not produce data (HTTP error, bad payload, not applicable here)."""


class NotApplicable(ProviderError):
    """The provider does not cover this location (e.g. NWS outside the US)."""
