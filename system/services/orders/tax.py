"""VAT calculation. v1 uses a per-country rate; v2 uses per-region rates (behind a flag)."""

from __future__ import annotations

VAT_BY_COUNTRY = {"PT": 0.23, "ES": 0.21, "FR": 0.20}

# Regional rates for the v2 calculation (ISO 3166-2 subdivision codes).
VAT_BY_REGION = {
    "PT-11": 0.23,
    "PT-13": 0.23,
    "PT-15": 0.23,
    "ES-MD": 0.21,
    "ES-CT": 0.21,
    "FR-IDF": 0.20,
}


def tax_v1(region: str, amount_cents: int) -> int:
    return round(amount_cents * VAT_BY_COUNTRY[region.split("-")[0]])


def tax_v2(region: str, amount_cents: int) -> int:
    return round(amount_cents * VAT_BY_REGION[region])


def compute(region: str, amount_cents: int, *, use_v2: bool) -> int:
    return tax_v2(region, amount_cents) if use_v2 else tax_v1(region, amount_cents)
