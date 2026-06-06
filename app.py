"""
MEF Subnational Efficiency — Auditoría del Gasto Público 2025 / Archivo 1964
============================================================================
4-tab Streamlit dashboard, co-authored by the Executor skill (data assembly
and base layout) and refined in place by the Evaluator/Optimizer skill
(caching, UX polish, divide-by-zero guards, quality-diff report in Tab 4).

This file reads ONLY the microscopic artifacts already produced under
data/processed/ by src/data_pipeline.py and src/ocr_engine.py — never a raw
multi-hundred-MB extract from datosabiertos.gob.pe (Critical Rule #1).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from analytical_engine import (  # noqa: E402
    historical_1964_lines_per_page,
    historical_1964_quantified_vs_structural,
    historical_1964_summary,
    modern_2025_advisor_narrative,
    modern_2025_by_department,
    modern_2025_hall_of_shame,
    modern_2025_kpis,
    modern_2025_locked_categories,
)
from utils import PROCESSED_DIR, fmt_pen, get_logger, parse_period, safe_div  # noqa: E402

log = get_logger("app")

st.set_page_config(
    page_title="MEF Subnational Efficiency — Auditoría 2025 / Archivo 1964",
    page_icon="🧭",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Evaluator-injected CSS polish — consistent palette, tighter metric cards,
# readable dataframes on both light/dark Streamlit themes.
# ---------------------------------------------------------------------------
st.markdown(
    """
    <style>
      .block-container {padding-top: 1.6rem; padding-bottom: 2rem;}
      div[data-testid="stMetric"] {
          background: rgba(120, 130, 150, 0.08);
          border: 1px solid rgba(120, 130, 150, 0.18);
          border-radius: 12px;
          padding: 0.85rem 1rem 0.6rem 1rem;
      }
      div[data-testid="stMetricLabel"] {font-weight: 600; opacity: 0.85;}
      .era-card {
          border-radius: 14px;
          padding: 1.1rem 1.3rem;
          border: 1px solid rgba(120,130,150,0.18);
          background: rgba(120,130,150,0.05);
          margin-bottom: 0.6rem;
      }
      .era-card h4 {margin-top: 0;}
      .audit-pill {
          display:inline-block; padding:0.15rem 0.6rem; border-radius:999px;
          font-size:0.78rem; font-weight:600; margin-right:0.4rem;
          background:rgba(46,160,67,0.16); color:#2ea043;
      }
      .audit-pill.warn {background:rgba(230,140,30,0.16); color:#e68c1e;}
    </style>
    """,
    unsafe_allow_html=True,
)

DEFAULT_PERIOD = "2025-12"

# Approximate department-capital coordinates for the geospatial tab — small,
# static lookup (no shapefiles / heavy geo dependencies needed for st/plotly).
DEPARTMENT_COORDS = {
    "Amazonas": (-6.23, -77.87), "Áncash": (-9.53, -77.53), "Apurímac": (-13.64, -72.88),
    "Arequipa": (-16.41, -71.54), "Ayacucho": (-13.16, -74.22), "Cajamarca": (-7.16, -78.51),
    "Callao": (-12.06, -77.14), "Cusco": (-13.53, -71.97), "Huancavelica": (-12.79, -74.97),
    "Huánuco": (-9.93, -76.24), "Ica": (-14.07, -75.73), "Junín": (-11.16, -75.99),
    "La Libertad": (-8.11, -79.03), "Lambayeque": (-6.70, -79.91), "Lima": (-12.05, -77.04),
    "Loreto": (-3.75, -73.25), "Madre de Dios": (-12.59, -69.19), "Moquegua": (-17.19, -70.93),
    "Pasco": (-10.68, -76.26), "Piura": (-5.19, -80.63), "Puno": (-15.84, -70.02),
    "San Martín": (-6.49, -76.37), "Tacna": (-18.01, -70.25), "Tumbes": (-3.57, -80.45),
    "Ucayali": (-8.38, -74.55),
}


# ---------------------------------------------------------------------------
# Cached loaders — Evaluator-enforced: every disk read is wrapped in
# @st.cache_data so re-rendering a tab costs (effectively) zero IO.
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False, ttl=600)
def load_2025_dataset(period_label: str) -> pd.DataFrame:
    """Load the microscopic per-unit 2025 fragment for `period_label`.
    Triggers the local executor pipeline on first access for a fresh period
    so the CLI's "redirect dynamically" behaviour also works from the UI's
    live sandbox (Tab 4)."""
    candidates = [
        PROCESSED_DIR / f"exec_2025_{period_label}.parquet",
        PROCESSED_DIR / f"exec_2025_{period_label}.csv",
    ]
    path = next((p for p in candidates if p.exists()), None)
    if path is None:
        _run_executor_for_period(period_label)
        path = next((p for p in candidates if p.exists()), None)
    if path is None:
        return pd.DataFrame()
    return pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path)


@st.cache_data(show_spinner=False, ttl=600)
def load_2025_summary(period_label: str) -> dict:
    path = PROCESSED_DIR / f"exec_2025_{period_label}_summary.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


@st.cache_data(show_spinner=False, ttl=3600)
def load_1964_lines() -> pd.DataFrame:
    for ext in (".parquet", ".csv"):
        path = PROCESSED_DIR / f"ocr_lines_1964{ext}"
        if path.exists():
            return pd.read_parquet(path) if ext == ".parquet" else pd.read_csv(path)
    return pd.DataFrame()


@st.cache_data(show_spinner=False, ttl=3600)
def load_1964_summary() -> dict:
    path = PROCESSED_DIR / "ocr_summary_1964.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


@st.cache_data(show_spinner=False, ttl=600)
def load_evaluator_report(period_label: str) -> str:
    for candidate in (
        PROCESSED_DIR / f"evaluator_report_{period_label}.md",
        PROCESSED_DIR / "evaluator_report.md",
    ):
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
    return (
        "_No se encontró un informe del Evaluador para este periodo todavía. "
        "Ejecuta `claude \"run evaluator_skill on the latest executor draft "
        f"for period {period_label}\"` para generarlo._"
    )


def _run_executor_for_period(period_label: str) -> None:
    """Live-sandbox hook (Tab 4): re-invokes the isolated local pipeline for
    a newly-requested period — mirrors exactly what `executor_skill` does
    when triggered via `claude "run executor_skill for period <X>"`, just
    without leaving the Streamlit process."""
    try:
        parsed = parse_period(period_label)
    except ValueError as exc:
        st.session_state["sandbox_error"] = str(exc)
        return
    try:
        from data_pipeline import run_period_pipeline

        run_period_pipeline(parsed)
        load_2025_dataset.clear()
        load_2025_summary.clear()
    except Exception as exc:  # pragma: no cover — surfaced in the sandbox UI
        log.exception("Sandbox re-run failed for period %s", period_label)
        st.session_state["sandbox_error"] = str(exc)


# ---------------------------------------------------------------------------
# Sidebar — global period control (drives Tabs 1-4 for the 2025 track; the
# 1964 track is intentionally period-independent — it is a fixed archive).
# ---------------------------------------------------------------------------
st.sidebar.title("🧭 MEF Subnational Efficiency")
st.sidebar.caption(
    "Auditoría multiagente del gasto público — FY2025 (MEF/SIAF) "
    "y digitalización del archivo histórico de 1964."
)
period_input = st.sidebar.text_input(
    "Periodo operativo (2025)",
    value=DEFAULT_PERIOD,
    help="Formatos aceptados: 'YYYY', 'YYYY-MM', 'YYYY-QN'. Igual al argumento "
    "que recibe `claude \"run executor_skill for period <periodo>\"`.",
)
try:
    active_period = parse_period(period_input).label
except ValueError:
    st.sidebar.error("Periodo inválido — usando 2025-12 por defecto.")
    active_period = DEFAULT_PERIOD

st.sidebar.divider()
st.sidebar.markdown(
    "**Arquitectura**\n"
    "- 🔌 Servidor MCP local (`src/mcp_server.py`)\n"
    "- 🛠️ Executor skill — ingeniería de datos\n"
    "- ⚖️ Evaluator/Optimizer skill — auditoría y QA\n"
    "- 🔎 PaddleOCR — archivo histórico de 1964\n"
)
st.sidebar.caption(
    "Esta app NUNCA lee CSV/JSON crudos del portal — solo fragmentos "
    "microscópicos en `data/processed/` generados localmente."
)

df_2025 = load_2025_dataset(active_period)
summary_2025 = load_2025_summary(active_period)
lines_1964 = load_1964_lines()
summary_1964 = load_1964_summary()

st.title("Auditoría del Gasto Público — Sistema Multiagente Local")
st.caption(
    f"Periodo activo: **{active_period}**  ·  Fuente 2025: "
    f"`{summary_2025.get('source', 'n/d')}`  ·  Fuente 1964: "
    f"`{summary_1964.get('source', 'n/d')}`"
)

tab1, tab2, tab3, tab4 = st.tabs([
    "📊 Resumen Macro · Doble Época",
    "🗺️ Distribución Territorial 2025",
    "🏛️ Salón de la Vergüenza · Anomalías 2025",
    "🧪 Auditoría Multiagente & Sandbox 2025",
])

# ===========================================================================
# TAB 1 — Macro Executive Summary & Dual-Era Opening Panel
#   Two fully INDEPENDENT containers (no cross-era formulas), per the brief.
# ===========================================================================
with tab1:
    st.markdown("### 🇵🇪 2025 — Ejecución Presupuestal Subnacional (MEF / SIAF)")
    st.markdown('<div class="era-card">', unsafe_allow_html=True)
    if df_2025.empty:
        st.warning(
            "No hay fragmento procesado para este periodo todavía. Ejecuta "
            f"`claude \"run executor_skill for period {active_period}\"` "
            "para generarlo (descarga vía MCP + agregación local aislada)."
        )
    else:
        kpis = modern_2025_kpis(df_2025)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("PIM total 2025", fmt_pen(kpis["total_pim"]))
        c2.metric("Devengado total 2025", fmt_pen(kpis["total_devengado"]))
        c3.metric("Tasa de ejecución nacional", f"{kpis['avance_pct']:.1f}%")
        c4.metric("Saldo no devengado (capital congelado)", fmt_pen(kpis["saldo_no_devengado"]))

        by_dept = modern_2025_by_department(df_2025)
        st.markdown("**🤖 Narrativa del asesor de IA — cuellos de botella fiscales modernos**")
        st.info(modern_2025_advisor_narrative(kpis, by_dept))
        st.caption(
            f"Fragmento basado en {summary_2025.get('n_units', len(df_2025))} unidades ejecutoras "
            f"con PIM > S/ 10M en {summary_2025.get('n_units_over_threshold', 'n/d')} de ellas "
            f"(fuente: `{summary_2025.get('source', 'n/d')}`, generado "
            f"{summary_2025.get('generated_at', 'n/d')[:19].replace('T', ' ')} UTC)."
        )
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown("### 📜 1964 — Archivo Histórico: Presupuesto, Balance y Cuenta General de la República")
    st.markdown('<div class="era-card">', unsafe_allow_html=True)
    if lines_1964.empty:
        st.warning(
            "Aún no se ha procesado el archivo de 1964. Ejecuta "
            "`claude \"run executor_skill for period 1964 --track historical\"` "
            "para descargar el PDF y disparar PaddleOCR sobre >= 15 páginas."
        )
    else:
        hist = historical_1964_summary(lines_1964)
        h1, h2, h3, h4 = st.columns(4)
        h1.metric("Páginas analizadas (PaddleOCR)", hist["pages_analyzed"])
        h2.metric("Líneas capturadas", hist["total_lines_captured"])
        h3.metric("Partidas cuantificadas", hist["quantified_entries"])
        h4.metric("Participación cuantificada", f"{hist['quantified_share_pct']:.1f}%")

        st.markdown(
            "**📝 Conclusiones extraídas del registro de 1964 (texto plano, fuente: "
            f"`{hist['source']}`)**"
        )
        st.markdown(
            f"- El analizador OCR procesó **{hist['pages_analyzed']} páginas** distintas de "
            f"matrices tabulares/textuales del Ministerio de Hacienda y Comercio (1964), "
            f"capturando **{hist['total_lines_captured']} líneas** de texto reconstruido, "
            f"con un promedio de **{hist['avg_lines_per_page']:.1f} líneas por página**.\n"
            f"- De ellas, **{hist['quantified_entries']} ({hist['quantified_share_pct']:.1f}%)** "
            "corresponden a partidas cuantificadas (montos en soles de oro / códigos de partida), "
            "mientras que el resto son encabezados estructurales — nombres de departamentos, "
            "rubros de ingreso/gasto y referencias de partida — que delimitan la organización "
            "contable de la época.\n"
            "- Esta lectura retrata cómo la Cuenta General de la República de 1964 organizaba "
            "el gasto por **departamento** y por **rubro funcional** (instrucción pública, "
            "obras públicas, fuerzas armadas, servicio de la deuda, fomento agropecuario), "
            "una estructura cualitativamente distinta a los clasificadores PIM/Devengado "
            "vigentes — por ello el sistema reporta esta época de forma **independiente**, "
            "sin fórmulas de comparación numérica contra 2025."
        )

        col_a, col_b = st.columns(2)
        with col_a:
            chart_lines = historical_1964_lines_per_page(lines_1964)
            fig_lines = px.bar(
                chart_lines, x="page", y=["quantified_lines", "textual_lines"],
                title="Líneas capturadas por página (1964) — densidad de matrices por folio",
                labels={"page": "Página del documento", "value": "Líneas OCR", "variable": "Tipo de línea"},
                color_discrete_map={"quantified_lines": "#2ea043", "textual_lines": "#6e7781"},
            )
            fig_lines.update_layout(legend_title_text="", height=380)
            st.plotly_chart(fig_lines, use_container_width=True)
        with col_b:
            chart_split = historical_1964_quantified_vs_structural(lines_1964)
            fig_split = px.pie(
                chart_split, names="categoria", values="conteo", hole=0.45,
                title="Distribución global: partidas cuantificadas vs. encabezados estructurales",
                color_discrete_sequence=["#2ea043", "#6e7781"],
            )
            fig_split.update_traces(textinfo="percent+value")
            fig_split.update_layout(height=380, legend=dict(orientation="h", y=-0.18))
            st.plotly_chart(fig_split, use_container_width=True)

        st.caption(
            "Nota metodológica: estas estadísticas describen ÚNICAMENTE lo que PaddleOCR "
            "reconstruyó de las páginas escaneadas del documento de 1964 — no se aplican "
            "fórmulas de avance/ejecución (esos clasificadores no existían en el formato "
            "de 1964) ni se comparan montos contra cifras de 2025."
        )
    st.markdown('</div>', unsafe_allow_html=True)

# ===========================================================================
# TAB 2 — Territorial Distribution & Geospatial Analysis (2025 ONLY)
# ===========================================================================
with tab2:
    st.markdown("### 🗺️ Desempeño territorial del gasto 2025 (exclusivamente datos modernos)")
    if df_2025.empty:
        st.warning("Sin datos 2025 cargados — genera el fragmento del periodo en la barra lateral / Tab 4.")
    else:
        by_dept = modern_2025_by_department(df_2025)
        by_dept["lat"] = by_dept["departamento"].map(lambda d: DEPARTMENT_COORDS.get(d, (None, None))[0])
        by_dept["lon"] = by_dept["departamento"].map(lambda d: DEPARTMENT_COORDS.get(d, (None, None))[1])
        geo_df = by_dept.dropna(subset=["lat", "lon"])

        col1, col2 = st.columns([1.15, 1])
        with col1:
            fig_map = px.scatter_mapbox(
                geo_df, lat="lat", lon="lon", size="pim", color="avance_pct",
                hover_name="departamento",
                hover_data={"pim": ":,.0f", "devengado": ":,.0f", "avance_pct": ":.1f", "lat": False, "lon": False},
                color_continuous_scale="RdYlGn", range_color=(0, 100), zoom=4.1,
                title="Ejecución 2025 por departamento (tamaño = PIM, color = avance %)",
            )
            fig_map.update_layout(
                mapbox_style="carto-positron",
                mapbox_center={"lat": -9.2, "lon": -75.0},
                height=520, margin=dict(l=0, r=0, t=50, b=0),
            )
            st.plotly_chart(fig_map, use_container_width=True)
        with col2:
            fig_heat = px.scatter(
                geo_df, x="avance_pct", y="social_vulnerability_index", size="saldo_no_devengado",
                color="stagnation_score", color_continuous_scale="OrRd", hover_name="departamento",
                labels={
                    "avance_pct": "Avance de ejecución 2025 (%)",
                    "social_vulnerability_index": "Índice de vulnerabilidad social",
                    "stagnation_score": "Score de estancamiento",
                },
                title="Mapa de calor: estancamiento presupuestal vs. vulnerabilidad social",
            )
            fig_heat.update_layout(height=520)
            st.plotly_chart(fig_heat, use_container_width=True)

        st.markdown("**Departamentos con mayor score de estancamiento (baja ejecución × alta vulnerabilidad)**")
        st.dataframe(
            by_dept[[
                "departamento", "pim", "devengado", "avance_pct",
                "saldo_no_devengado", "social_vulnerability_index", "stagnation_score",
            ]].head(10).style.format({
                "pim": "S/ {:,.0f}", "devengado": "S/ {:,.0f}", "saldo_no_devengado": "S/ {:,.0f}",
                "avance_pct": "{:.1f}%", "social_vulnerability_index": "{:.2f}", "stagnation_score": "{:.3f}",
            }),
            use_container_width=True, hide_index=True,
        )

# ===========================================================================
# TAB 3 — Budget "Hall of Shame" & Anomaly Explorer (2025 ONLY)
# ===========================================================================
with tab3:
    st.markdown("### 🏛️ Unidades ejecutoras con peor desempeño (PIM > S/ 10 millones)")
    if df_2025.empty:
        st.warning("Sin datos 2025 cargados — genera el fragmento del periodo en la barra lateral / Tab 4.")
    else:
        hall = modern_2025_hall_of_shame(df_2025)
        if hall.empty:
            st.success("No se encontraron unidades con PIM > S/ 10M en este fragmento — sin anomalías que reportar.")
        else:
            st.dataframe(
                hall.style.format({
                    "pim": "S/ {:,.0f}", "devengado": "S/ {:,.0f}", "saldo_no_devengado": "S/ {:,.0f}",
                    "avance_pct": "{:.1f}%",
                }).background_gradient(subset=["avance_pct"], cmap="RdYlGn", vmin=0, vmax=100),
                use_container_width=True, hide_index=True, height=420,
            )
            st.caption(
                f"{len(hall)} unidades mostradas de "
                f"{summary_2025.get('n_units_over_threshold', 'n/d')} con presupuesto crítico "
                f"(> S/ 10M) y {summary_2025.get('n_underperformers', 'n/d')} clasificadas como "
                "subejecutoras (< 50% de avance)."
            )

        st.markdown("**Categorías de gasto bloqueadas — distribución del saldo no devengado**")
        locked = modern_2025_locked_categories(df_2025)
        if locked.empty:
            st.info("No hay categorías con saldo bloqueado significativo (avance < 50%) en este fragmento.")
        else:
            fig_locked = px.bar(
                locked, x="saldo_no_devengado", y="categoria_gasto_bloqueada", orientation="h",
                color="n_unidades", color_continuous_scale="OrRd",
                labels={
                    "saldo_no_devengado": "Saldo no devengado (S/)",
                    "categoria_gasto_bloqueada": "",
                    "n_unidades": "N° unidades",
                },
                title="¿Dónde está el capital congelado? — por categoría de gasto",
            )
            fig_locked.update_layout(height=430, yaxis=dict(autorange="reversed"))
            st.plotly_chart(fig_locked, use_container_width=True)

# ===========================================================================
# TAB 4 — Multi-Agent Audit Log & Live Sandbox (2025 ONLY)
# ===========================================================================
with tab4:
    st.markdown("### 🧪 Bitácora de auditoría multiagente (Executor → Evaluator/Optimizer)")
    st.markdown(
        '<span class="audit-pill">Executor: borrador generado</span>'
        '<span class="audit-pill">Evaluator: QA + optimización aplicada</span>'
        '<span class="audit-pill warn">Estado: revisar informe abajo</span>',
        unsafe_allow_html=True,
    )
    report_md = load_evaluator_report(active_period)
    with st.expander("📄 Informe de calidad del Evaluador/Optimizer (markdown crudo renderizado)", expanded=True):
        st.markdown(report_md)

    st.divider()
    st.markdown("### 🛠️ Sandbox interactivo — re-ejecutar el pipeline por periodo (CLI ↔ UI)")
    st.caption(
        "Replica exactamente lo que dispara `claude \"run executor_skill for period <periodo>\"`: "
        "re-descarga/agrega el fragmento del periodo vía el script local aislado "
        "`src/data_pipeline.py` y refresca el estado de la app sin reiniciar el proceso."
    )
    sandbox_col1, sandbox_col2 = st.columns([2, 1])
    with sandbox_col1:
        sandbox_period = st.text_input(
            "Periodo a (re)generar", value=active_period, key="sandbox_period_input",
            help="Acepta 'YYYY', 'YYYY-MM', 'YYYY-QN' — idéntico al argumento de la CLI.",
        )
    with sandbox_col2:
        st.write("")
        st.write("")
        run_clicked = st.button("▶️ Ejecutar pipeline local", use_container_width=True)

    if run_clicked:
        st.session_state.pop("sandbox_error", None)
        with st.spinner(f"Ejecutando `data_pipeline.run_period_pipeline` para {sandbox_period}…"):
            _run_executor_for_period(sandbox_period)
        if st.session_state.get("sandbox_error"):
            st.error(f"No se pudo regenerar el periodo: {st.session_state['sandbox_error']}")
        else:
            st.success(
                f"Fragmento regenerado para `{sandbox_period}`. Cambia el periodo en la barra "
                "lateral al mismo valor para ver el dashboard actualizado (cachés invalidadas)."
            )

    st.divider()
    st.markdown("##### 🔎 Resumen crudo del último ciclo (Executor → Evaluator)")
    raw_col1, raw_col2 = st.columns(2)
    with raw_col1:
        st.markdown("**Resumen del Executor — `exec_2025_<periodo>_summary.json`**")
        st.json(summary_2025 or {"info": "sin datos para este periodo todavía"})
    with raw_col2:
        st.markdown("**Resumen OCR del Executor — `ocr_summary_1964.json`**")
        st.json(summary_1964 or {"info": "OCR de 1964 aún no ejecutado"})
