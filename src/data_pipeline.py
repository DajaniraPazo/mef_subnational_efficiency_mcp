"""
Local, isolated data-engineering worker scripts — the "anti-context-flood"
half of the architecture (Critical Rule #1).

Claude Code never ingests the MEF/SIAF 2025 CSV/JSON extracts (200MB-1GB)
directly. Instead it:
  1. Calls `inspeccionar_esquema_csv` (MCP) to capture a 5-10 row schema
     snapshot -> persisted under data/snapshots/.
  2. Triggers THIS module (via `descargar_y_analizar_estadisticas` / the
     executor skill / `claude "run executor_skill for period 2025-12"`),
     which downloads, filters, aggregates and writes a microscopic Parquet
     fragment under data/processed/ — the only thing app.py ever reads.

Because the public CKAN datastore for the 2025 subnational execution extract
is not guaranteed to be reachable from every network (geo-fencing / WAF on
datosabiertos.gob.pe is common), this module degrades gracefully: it first
attempts a live datastore pull, and if that is unavailable it falls back to a
seeded, fully-deterministic synthetic generator that mirrors the *real*
MEF "Consulta Amigable" schema (pliego, ejecutora, departamento, sector,
genérica de gasto, PIM, Devengado). Every artifact records its `source`
field ("live_datastore" | "synthetic_fallback") so the Evaluator skill's
audit trail stays honest about provenance.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import requests

from utils import (
    CKAN_DATASTORE_SEARCH,
    CKAN_PACKAGE_SEARCH,
    PROCESSED_DIR,
    SNAPSHOT_DIR,
    Period,
    get_logger,
    safe_div,
)

log = get_logger("data_pipeline")

_HTTP_TIMEOUT = 30
_SEED = 20250101  # deterministic — re-runs for the same period reproduce the same snapshot

DEPARTMENTS = [
    "Amazonas", "Áncash", "Apurímac", "Arequipa", "Ayacucho", "Cajamarca",
    "Callao", "Cusco", "Huancavelica", "Huánuco", "Ica", "Junín",
    "La Libertad", "Lambayeque", "Lima", "Loreto", "Madre de Dios",
    "Moquegua", "Pasco", "Piura", "Puno", "San Martín", "Tacna",
    "Tumbes", "Ucayali",
]

# Approx. multidimensional poverty incidence (illustrative weighting only —
# used to build the "social vulnerability" overlay on the geospatial tab,
# NOT presented as an official INEI figure).
SOCIAL_VULNERABILITY_INDEX = {
    "Amazonas": 0.78, "Áncash": 0.46, "Apurímac": 0.71, "Arequipa": 0.28,
    "Ayacucho": 0.69, "Cajamarca": 0.74, "Callao": 0.18, "Cusco": 0.58,
    "Huancavelica": 0.83, "Huánuco": 0.72, "Ica": 0.24, "Junín": 0.49,
    "La Libertad": 0.41, "Lambayeque": 0.43, "Lima": 0.14, "Loreto": 0.76,
    "Madre de Dios": 0.45, "Moquegua": 0.22, "Pasco": 0.61, "Piura": 0.52,
    "Puno": 0.66, "San Martín": 0.56, "Tacna": 0.20, "Tumbes": 0.39,
    "Ucayali": 0.62,
}

GOV_LEVELS = ["GOBIERNO REGIONAL", "GOBIERNO LOCAL"]
SECTORS = [
    "Educación", "Salud", "Transporte", "Saneamiento", "Agricultura",
    "Vivienda y Urbanismo", "Energía y Minas", "Orden Público y Seguridad",
    "Ambiente", "Cultura y Deporte",
]
SPENDING_CATEGORIES = [
    "Infraestructura de concreto (obras viales/edificaciones)",
    "Adquisición de maquinaria y equipo",
    "Consultorías y estudios de preinversión",
    "Mantenimiento de infraestructura existente",
    "Programas sociales y subsidios directos",
    "Adquisición de bienes y suministros",
    "Servicios básicos y operación corriente",
    "Capacitación y desarrollo de capacidades",
]

CRITICAL_BUDGET_THRESHOLD_PEN = 10_000_000.0  # "> S/ 10 millones" focus from the brief


# ---------------------------------------------------------------------------
# Live datastore attempt (kept thin & defensive — never raises to the caller)
# ---------------------------------------------------------------------------
def _try_live_datastore(period: Period, limit: int = 200) -> pd.DataFrame | None:
    """Best-effort pull of a small, server-side-filtered slice of the public
    2025 subnational execution datastore. Returns None on any failure so the
    pipeline can fall back to the synthetic generator without crashing the
    Skill invocation."""
    try:
        search = requests.get(
            CKAN_PACKAGE_SEARCH,
            params={"q": f"presupuesto ejecucion gasto {period.year} gobiernos regionales locales", "rows": 3},
            timeout=_HTTP_TIMEOUT,
        )
        search.raise_for_status()
        packages = search.json().get("result", {}).get("results", [])
        for pkg in packages:
            for resource in pkg.get("resources", []):
                if not resource.get("datastore_active"):
                    continue
                ds = requests.get(
                    CKAN_DATASTORE_SEARCH,
                    params={"resource_id": resource["id"], "limit": limit},
                    timeout=_HTTP_TIMEOUT,
                )
                ds.raise_for_status()
                records = ds.json().get("result", {}).get("records", [])
                if records:
                    log.info("Live datastore hit: resource_id=%s rows=%d", resource["id"], len(records))
                    return pd.DataFrame.from_records(records)
    except (requests.RequestException, ValueError, KeyError) as exc:
        log.info("Live datastore unavailable (%s) — using synthetic fallback.", exc)
    return None


# ---------------------------------------------------------------------------
# Synthetic, seeded fallback generator — mirrors MEF "Consulta Amigable" shape
# ---------------------------------------------------------------------------
def _generate_synthetic_2025(period: Period) -> pd.DataFrame:
    rng = random.Random(_SEED + period.year + (period.month or 0) + (period.quarter or 0))
    rows: list[dict[str, Any]] = []
    unit_id = 1
    for dept in DEPARTMENTS:
        n_units = rng.randint(3, 6)
        for _ in range(n_units):
            level = rng.choices(GOV_LEVELS, weights=[0.35, 0.65])[0]
            sector = rng.choice(SECTORS)
            pim = round(rng.uniform(2_000_000, 85_000_000), 2)
            # Skew execution rates so a realistic "long tail" of underperformers exists
            base_rate = rng.betavariate(2.1, 1.6)
            avance = max(0.04, min(0.99, base_rate))
            devengado = round(pim * avance, 2)
            category = rng.choice(SPENDING_CATEGORIES)
            rows.append({
                "anio_eje": period.year,
                "departamento": dept,
                "nivel_gobierno": level,
                "sector": sector,
                "unidad_ejecutora": f"{dept[:3].upper()}-{level.split()[1][:3]}-{unit_id:04d}",
                "pliego": f"Pliego {dept} #{unit_id:04d}",
                "pim": pim,
                "devengado": devengado,
                "categoria_gasto_bloqueada": category,
                "social_vulnerability_index": SOCIAL_VULNERABILITY_INDEX[dept],
            })
            unit_id += 1
    df = pd.DataFrame(rows)
    df["saldo_no_devengado"] = df["pim"] - df["devengado"]
    df["avance_pct"] = (df["devengado"] / df["pim"] * 100).round(2)
    return df


# ---------------------------------------------------------------------------
# Aggregation + microscopic-footprint persistence
# ---------------------------------------------------------------------------
def _persist_snapshot(df_sample: pd.DataFrame, period: Period) -> str:
    """Persist a 5-10 row SCHEMA snapshot (never the full frame) for audit."""
    path = SNAPSHOT_DIR / f"schema_2025_{period.label}.json"
    payload = {
        "period": period.label,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "columns": list(df_sample.columns),
        "dtypes": {c: str(t) for c, t in df_sample.dtypes.items()},
        "sample_rows": df_sample.head(10).to_dict(orient="records"),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return str(path)


def _persist_processed(df: pd.DataFrame, name: str) -> str:
    """Persist the microscopic, already-aggregated artifact app.py reads."""
    path = PROCESSED_DIR / f"{name}.parquet"
    try:
        df.to_parquet(path, index=False)
    except (ImportError, ValueError):  # pyarrow missing in a minimal env
        path = PROCESSED_DIR / f"{name}.csv"
        df.to_csv(path, index=False)
    return str(path)


def run_period_pipeline(period: Period, focus: str = "ejecucion") -> dict[str, Any]:
    """Entry point invoked by the MCP tool `descargar_y_analizar_estadisticas`
    and by `executor_skill` for `claude "run executor_skill for period 2025-12"`.

    Produces (and overwrites, so re-runs are idempotent):
      data/snapshots/schema_2025_<period>.json     — schema + 10-row sample
      data/processed/exec_2025_<period>.parquet    — full per-unit fragment (small: ~120 rows)
      data/processed/exec_2025_<period>_summary.json — descriptive aggregates

    Returns a small dict of descriptive statistics — never the dataframe.
    """
    log.info("Running executor pipeline for period=%s focus=%s", period.label, focus)

    live_df = _try_live_datastore(period)
    if live_df is not None and not live_df.empty:
        df = live_df
        source = "live_datastore"
    else:
        df = _generate_synthetic_2025(period)
        source = "synthetic_fallback"

    snapshot_path = _persist_snapshot(df, period)
    artifact_path = _persist_processed(df, f"exec_2025_{period.label}")

    national_pim = float(df["pim"].sum())
    national_devengado = float(df["devengado"].sum())
    national_avance = round(safe_div(national_devengado, national_pim) * 100, 2)
    frozen_capital = float((df["pim"] - df["devengado"]).clip(lower=0).sum())

    critical = df[df["pim"] > CRITICAL_BUDGET_THRESHOLD_PEN]
    underperformers = critical[critical["avance_pct"] < 50.0]

    summary = {
        "source": source,
        "n_units": int(len(df)),
        "n_units_over_threshold": int(len(critical)),
        "n_underperformers": int(len(underperformers)),
        "national_pim": national_pim,
        "national_devengado": national_devengado,
        "national_avance_pct": national_avance,
        "frozen_capital_pen": frozen_capital,
        "snapshot_path": snapshot_path,
        "processed_path": artifact_path,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    summary_path = PROCESSED_DIR / f"exec_2025_{period.label}_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["summary_path"] = str(summary_path)

    log.info(
        "Pipeline complete: source=%s units=%d national_avance=%.2f%% frozen_capital=S/ %.2f",
        source, len(df), national_avance, frozen_capital,
    )
    return summary


# ---------------------------------------------------------------------------
# CLI entry — lets the executor skill (or a human) re-run the worker directly:
#   python src/data_pipeline.py 2025-12
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    from utils import parse_period

    raw_period = sys.argv[1] if len(sys.argv) > 1 else "2025-12"
    result = run_period_pipeline(parse_period(raw_period))
    print(json.dumps(result, ensure_ascii=False, indent=2))
