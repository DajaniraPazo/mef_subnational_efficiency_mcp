"""
Core metric & grouping modules shared by the executor pipeline and app.py.

Two independent metric families live here, matching the brief's explicit
instruction NOT to force cross-era comparisons (1964 and 2025 use entirely
different recording frameworks):

  * `modern_2025_*`    — Avance % (Devengado / PIM), Saldo No Devengado
                          ("frozen capital"), Hall-of-Shame ranking, category
                          breakdowns, geospatial aggregation.
  * `historical_1964_*`— descriptive statistics computed strictly from what
                          PaddleOCR actually extracted (line counts, quantified
                          items, department/rubro distributions) — no synthetic
                          numerical comparison to 2025 PEN values is produced.

Every function takes/returns small, already-aggregated pandas objects —
this module never touches raw multi-hundred-MB extracts; that boundary is
enforced upstream in data_pipeline.py / ocr_engine.py.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from utils import safe_div

CRITICAL_BUDGET_THRESHOLD_PEN = 10_000_000.0


# ---------------------------------------------------------------------------
# Modern (2025) track — Reference metrics from the brief
# ---------------------------------------------------------------------------
def modern_2025_kpis(df: pd.DataFrame) -> dict[str, float]:
    """National-level KPI block for Tab 1: total PIM, total Devengado,
    Avance % = (Devengado / PIM) * 100, and Saldo No Devengado."""
    pim = float(df["pim"].sum())
    devengado = float(df["devengado"].sum())
    return {
        "total_pim": pim,
        "total_devengado": devengado,
        "avance_pct": round(safe_div(devengado, pim) * 100, 2),
        "saldo_no_devengado": float(pim - devengado),
    }


def modern_2025_by_department(df: pd.DataFrame) -> pd.DataFrame:
    """Department-level rollup for Tab 2 (geospatial / heatmap)."""
    grouped = (
        df.groupby("departamento", as_index=False)
        .agg(
            pim=("pim", "sum"),
            devengado=("devengado", "sum"),
            n_unidades=("unidad_ejecutora", "count"),
            social_vulnerability_index=("social_vulnerability_index", "mean"),
        )
    )
    grouped["avance_pct"] = (grouped["devengado"] / grouped["pim"] * 100).round(2)
    grouped["saldo_no_devengado"] = grouped["pim"] - grouped["devengado"]
    grouped["stagnation_score"] = (
        (100 - grouped["avance_pct"]).clip(lower=0) / 100 * grouped["social_vulnerability_index"]
    ).round(4)
    return grouped.sort_values("stagnation_score", ascending=False).reset_index(drop=True)


def modern_2025_hall_of_shame(df: pd.DataFrame, min_budget: float = CRITICAL_BUDGET_THRESHOLD_PEN, top_n: int = 25) -> pd.DataFrame:
    """Tab 3 — worst-performing execution units with PIM > S/ 10M, ranked by
    lowest Avance % (i.e. the most "frozen capital" relative to budget size)."""
    subset = df[df["pim"] > min_budget].copy()
    subset["saldo_no_devengado"] = subset["pim"] - subset["devengado"]
    cols = [
        "unidad_ejecutora", "pliego", "departamento", "nivel_gobierno", "sector",
        "pim", "devengado", "avance_pct", "saldo_no_devengado", "categoria_gasto_bloqueada",
    ]
    return (
        subset[cols]
        .sort_values(["avance_pct", "saldo_no_devengado"], ascending=[True, False])
        .head(top_n)
        .reset_index(drop=True)
    )


def modern_2025_locked_categories(df: pd.DataFrame, min_budget: float = CRITICAL_BUDGET_THRESHOLD_PEN) -> pd.DataFrame:
    """Category breakdown of "locked" spend (PIM > 10M & Avance < 50%) for
    the Tab 3 visual breakdown (e.g. concrete infrastructure, machinery)."""
    subset = df[(df["pim"] > min_budget) & (df["avance_pct"] < 50.0)].copy()
    subset["saldo_no_devengado"] = subset["pim"] - subset["devengado"]
    grouped = (
        subset.groupby("categoria_gasto_bloqueada", as_index=False)
        .agg(
            n_unidades=("unidad_ejecutora", "count"),
            saldo_no_devengado=("saldo_no_devengado", "sum"),
            pim=("pim", "sum"),
        )
        .sort_values("saldo_no_devengado", ascending=False)
        .reset_index(drop=True)
    )
    return grouped


def modern_2025_advisor_narrative(kpis: dict[str, float], by_dept: pd.DataFrame) -> str:
    """Short AI-advisor narrative for Tab 1 — built deterministically from the
    already-computed aggregates (no extra LLM round-trip needed at render time)."""
    worst = by_dept.iloc[0] if not by_dept.empty else None
    best = by_dept.sort_values("avance_pct", ascending=False).iloc[0] if not by_dept.empty else None
    bits = [
        f"La ejecución nacional 2025 alcanza un avance de **{kpis['avance_pct']:.1f}%** "
        f"(Devengado S/ {kpis['total_devengado']:,.0f} sobre un PIM de S/ {kpis['total_pim']:,.0f}), "
        f"dejando **S/ {kpis['saldo_no_devengado']:,.0f}** en saldo no devengado — "
        "capital presupuestal formalmente asignado pero aún sin convertirse en gasto devengado."
    ]
    if worst is not None:
        bits.append(
            f"El cuello de botella más crítico se concentra en **{worst['departamento']}**, "
            f"donde el índice de estancamiento ({worst['stagnation_score']:.3f}) combina baja "
            f"ejecución ({worst['avance_pct']:.1f}%) con alta vulnerabilidad social — "
            "una combinación que la auditoría debe priorizar."
        )
    if best is not None:
        bits.append(
            f"En contraste, **{best['departamento']}** exhibe el mejor avance "
            f"({best['avance_pct']:.1f}%), sugiriendo prácticas de gestión replicables "
            "en unidades ejecutoras comparables."
        )
    return " ".join(bits)


# ---------------------------------------------------------------------------
# Historical (1964) track — descriptive stats strictly from OCR'd lines
# ---------------------------------------------------------------------------
def historical_1964_summary(lines_df: pd.DataFrame) -> dict[str, Any]:
    """Descriptive summary computed ONLY from what PaddleOCR captured on the
    >= 15 processed pages — no numerical bridge to the 2025 PEN figures."""
    n_pages = int(lines_df["page"].nunique())
    numeric = lines_df[lines_df["looks_numeric"]]
    textual = lines_df[~lines_df["looks_numeric"]]
    return {
        "pages_analyzed": n_pages,
        "total_lines_captured": int(len(lines_df)),
        "quantified_entries": int(len(numeric)),
        "structural_text_entries": int(len(textual)),
        "quantified_share_pct": round(safe_div(len(numeric), len(lines_df)) * 100, 1),
        "avg_lines_per_page": round(safe_div(len(lines_df), n_pages), 1),
        "source": str(lines_df["source"].iloc[0]) if len(lines_df) else "unknown",
    }


def historical_1964_lines_per_page(lines_df: pd.DataFrame) -> pd.DataFrame:
    """Chart #1 input — line volume captured per scanned page (shows where
    the densest tabular matrices were found in the 1964 ledger)."""
    grouped = (
        lines_df.groupby("page", as_index=False)
        .agg(
            total_lines=("text", "count"),
            quantified_lines=("looks_numeric", "sum"),
        )
        .sort_values("page")
        .reset_index(drop=True)
    )
    grouped["textual_lines"] = grouped["total_lines"] - grouped["quantified_lines"]
    return grouped


def historical_1964_quantified_vs_structural(lines_df: pd.DataFrame) -> pd.DataFrame:
    """Chart #2 input — overall split between quantified (numeric ledger
    entries) vs. structural/textual lines (department & rubro headers)
    across all processed pages, for a distribution pie/bar."""
    numeric = int(lines_df["looks_numeric"].sum())
    textual = int(len(lines_df) - numeric)
    return pd.DataFrame(
        {
            "categoria": ["Partidas cuantificadas (montos / códigos)", "Encabezados estructurales (rubros / departamentos)"],
            "conteo": [numeric, textual],
        }
    )
