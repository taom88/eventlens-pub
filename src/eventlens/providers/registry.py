"""Free-first provider construction and an explicit routing plan."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from eventlens.config import EventLensSettings

from .alpha_vantage import AlphaVantageProvider
from .finnhub import FinnhubProvider
from .fixture import FixtureProvider
from .http import CachedHttpClient
from .massive import MassiveProvider
from .sec_edgar import SecEdgarProvider, valid_sec_user_agent
from .yfinance_provider import YFinanceProvider


class ProviderPlanEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    capability: str
    priority: int
    provider: str
    enabled: bool
    quota_cost: str
    rationale: str


class FreeFirstProviderRegistry:
    """Concrete providers plus a visible, non-magical source priority plan."""

    def __init__(
        self,
        *,
        yfinance: YFinanceProvider | None,
        sec: SecEdgarProvider | None,
        alpha_vantage: AlphaVantageProvider | None,
        finnhub: FinnhubProvider | None,
        massive: MassiveProvider | None,
        fixture: FixtureProvider | None,
    ) -> None:
        self.yfinance = yfinance
        self.sec = sec
        self.alpha_vantage = alpha_vantage
        self.finnhub = finnhub
        self.massive = massive
        self.fixture = fixture

    def plan(self) -> tuple[ProviderPlanEntry, ...]:
        return (
            ProviderPlanEntry(
                capability="ticker news sentiment",
                priority=1,
                provider="Massive",
                enabled=self.massive is not None,
                quota_cost="one cached call/window",
                rationale="Primary rights-safe ticker-news metadata and native sentiment labels.",
            ),
            ProviderPlanEntry(
                capability="daily adjusted OHLCV",
                priority=1,
                provider="yfinance",
                enabled=self.yfinance is not None,
                quota_cost="no key",
                rationale="Default US-equity history; avoids both confirmed key quotas.",
            ),
            ProviderPlanEntry(
                capability="historical earnings filing evidence",
                priority=1,
                provider="SEC EDGAR",
                enabled=self.sec is not None,
                quota_cost="no key; fair-access limits",
                rationale="Authoritative Item 2.02 filing evidence.",
            ),
            ProviderPlanEntry(
                capability="near-term earnings date/time",
                priority=1,
                provider="Finnhub",
                enabled=self.finnhub is not None,
                quota_cost="one cached call/window",
                rationale="Free tier supplies one month of US calendar data including BMO/AMC.",
            ),
            ProviderPlanEntry(
                capability="earnings transcript",
                priority=1,
                provider="Alpha Vantage",
                enabled=self.alpha_vantage is not None,
                quota_cost="one cached call/quarter",
                rationale="15+ years; avoids Finnhub's premium transcript endpoint.",
            ),
            ProviderPlanEntry(
                capability="earnings history/EPS cross-check",
                priority=2,
                provider="Alpha Vantage",
                enabled=self.alpha_vantage is not None,
                quota_cost="one cached call/ticker",
                rationale="Long-lived cache amortizes one request across multiple events.",
            ),
            ProviderPlanEntry(
                capability="historical options",
                priority=1,
                provider="fixture",
                enabled=self.fixture is not None,
                quota_cost="offline",
                rationale="Alpha Vantage and Finnhub historical option coverage is paid.",
            ),
        )


def build_free_first_registry(
    settings: EventLensSettings,
    *,
    fixture_root: Path | None = None,
    http: CachedHttpClient | None = None,
) -> FreeFirstProviderRegistry:
    client = http or CachedHttpClient(settings.provider_cache_dir)
    alpha = None
    if settings.alpha_vantage_api_key is not None:
        alpha = AlphaVantageProvider(
            settings.alpha_vantage_api_key.get_secret_value(),
            client,
            daily_budget=settings.alpha_vantage_daily_budget,
        )
    finnhub = None
    if settings.finnhub_api_key is not None:
        finnhub = FinnhubProvider(
            settings.finnhub_api_key.get_secret_value(),
            client,
            daily_budget=settings.finnhub_daily_budget,
        )
    massive = None
    if settings.massive_api_key is not None:
        massive = MassiveProvider(settings.massive_api_key.get_secret_value(), client)
    sec = (
        SecEdgarProvider(settings.sec_user_agent, client)
        if valid_sec_user_agent(settings.sec_user_agent)
        else None
    )
    return FreeFirstProviderRegistry(
        yfinance=YFinanceProvider() if settings.enable_yfinance else None,
        sec=sec,
        alpha_vantage=alpha,
        finnhub=finnhub,
        massive=massive,
        fixture=FixtureProvider(fixture_root) if fixture_root is not None else None,
    )
