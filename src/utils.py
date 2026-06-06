"""
Shared helpers for the MEF Subnational Efficiency pipeline: filesystem layout,
structured logging and small parsing utilities used by every module so that
the MCP server, the executor pipeline, the OCR engine and the Streamlit app
all agree on *where things live* and *how they talk about periods*.
"""
from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Repository layout (single source of truth — every other module imports this)
# ---------------------------------------------------------------------------
ROOT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT_DIR / "src"
DATA_DIR = ROOT_DIR / "data"
RAW_PDF_DIR = DATA_DIR / "raw_pdfs"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
PROCESSED_DIR = DATA_DIR / "processed"
SKILLS_DIR = ROOT_DIR / ".claude" / "skills"

for _d in (RAW_PDF_DIR, SNAPSHOT_DIR, PROCESSED_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Portal de Datos Abiertos del Perú — CKAN endpoints used by src/mcp_server.py
PORTAL_BASE_URL = "https://www.datosabiertos.gob.pe"
CKAN_PACKAGE_SEARCH = f"{PORTAL_BASE_URL}/api/3/action/package_search"
CKAN_PACKAGE_SHOW = f"{PORTAL_BASE_URL}/api/3/action/package_show"
CKAN_DATASTORE_SEARCH = f"{PORTAL_BASE_URL}/api/3/action/datastore_search"

# Source for the 1964 historical fiscal record (Track 1964)
HISTORICAL_SOURCE_URL = (
    "https://fuenteshistoricasdelperu.com/2021/08/12/"
    "ministerio-de-hacienda-y-comercio-presupuesto-balance-y-cuenta-general-de-la-republica/"
)
HISTORICAL_PDF_PATH = RAW_PDF_DIR / "mef_1964_presupuesto_balance_cuenta_general.pdf"
MIN_OCR_PAGES = 15


# ---------------------------------------------------------------------------
# Logging — every module pulls a namespaced logger from here so a single run
# produces one coherent, timestamped trace that the Evaluator skill can audit.
# ---------------------------------------------------------------------------
_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_configured = False


def get_logger(name: str) -> logging.Logger:
    global _configured
    if not _configured:
        logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT, stream=sys.stdout)
        _configured = True
    return logging.getLogger(name)


# ---------------------------------------------------------------------------
# Period parsing — translates CLI-supplied strings such as "2025-12",
# "2025-Q4" or "2025" into a normalized (year, label, quarter|None) tuple so
# the executor skill can "redirect dynamically" without hard-coded dates.
# ---------------------------------------------------------------------------
_PERIOD_RE = re.compile(
    r"^(?P<year>\d{4})(?:-(?:(?P<month>\d{2})|Q(?P<quarter>[1-4])))?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Period:
    year: int
    label: str
    month: int | None = None
    quarter: int | None = None

    @property
    def quarter_from_month(self) -> int | None:
        if self.month:
            return (self.month - 1) // 3 + 1
        return self.quarter


def parse_period(raw: str) -> Period:
    """Parse user-supplied period strings like '2025-12', '2025-Q4', '2025'.

    Raises ValueError on malformed input — callers (CLI / MCP tools) surface
    that directly so the user gets immediate, actionable feedback instead of
    the pipeline silently defaulting to a stale period.
    """
    raw = raw.strip()
    match = _PERIOD_RE.match(raw)
    if not match:
        raise ValueError(
            f"Invalid period '{raw}'. Expected formats: 'YYYY', 'YYYY-MM' or 'YYYY-QN'."
        )
    year = int(match.group("year"))
    month = int(match.group("month")) if match.group("month") else None
    quarter = int(match.group("quarter")) if match.group("quarter") else None
    return Period(year=year, label=raw, month=month, quarter=quarter)


def safe_div(numerator: float, denominator: float) -> float:
    """Division helper that neutralizes the divide-by-zero crashes the
    Evaluator skill is tasked with eliminating from the dashboard."""
    if denominator in (0, None) or denominator != denominator:  # NaN check
        return 0.0
    return numerator / denominator


def fmt_pen(value: float) -> str:
    """Format a sol amount in millions with thousands separators, e.g. 'S/ 12.4M'."""
    try:
        return f"S/ {value / 1_000_000:,.1f}M"
    except (TypeError, ZeroDivisionError):
        return "S/ 0.0M"
