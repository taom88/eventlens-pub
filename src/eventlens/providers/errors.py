"""Provider failures with stable, user-facing semantics."""


class ProviderError(RuntimeError):
    """Base class for normalized provider failures."""


class ProviderConfigurationError(ProviderError):
    """A required key, contact identity, or optional dependency is missing."""


class ProviderEntitlementError(ProviderError):
    """The configured account does not include the requested endpoint."""


class ProviderPayloadError(ProviderError):
    """The upstream returned JSON, but not the documented payload shape."""
