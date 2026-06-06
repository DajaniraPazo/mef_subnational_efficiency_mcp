"""
Local MCP server for the MEF Subnational Efficiency pipeline.

Exposes a small, deliberately-scoped toolkit that lets Claude Code talk to
the Portal de Datos Abiertos del Perú (CKAN, datosabiertos.gob.pe) and to the
1964 historical-archive track WITHOUT ever pulling a raw 200MB-1GB CSV/JSON
into the model's context window (see Critical Rule #1 in the assignment).

Every tool here returns either:
  * a thin metadata/schema preview (a handful of rows / fields), or
  * a path to a microscopic artifact that a *local* script (data_pipeline.py /
    ocr_engine.py / analytical_engine.py) already produced on disk.

Run standalone for local debugging:
    python src/mcp_server.py

Wire into Claude Code via .mcp.json:
    {
      "mcpServers": {
        "mef-subnational": {
          "command": "python",
          "args": ["src/mcp_server.py"]
        }
      }
    }
"""
from __future__ import annotations

import json
from typing import Any

import requests

from mcp.server.fastmcp import FastMCP

from utils import (
    CKAN_DATASTORE_SEARCH,
    CKAN_PACKAGE_SEARCH,
    CKAN_PACKAGE_SHOW,
    HISTORICAL_PDF_PATH,
    HISTORICAL_SOURCE_URL,
    MIN_OCR_PAGES,
    PROCESSED_DIR,
    RAW_PDF_DIR,
    SNAPSHOT_DIR,
    get_logger,
    parse_period,
)

log = get_logger("mcp_server")

mcp = FastMCP(
    "mef-subnational-efficiency",
    instructions=(
        "Tools for auditing the 2025 Peruvian public budget (MEF/SIAF, via "
        "datosabiertos.gob.pe) and digitizing the 1964 historical fiscal "
        "record. NEVER stream full datasets through these tools — only "
        "schemas, samples, aggregated summaries and file paths to local "
        "microscopic artifacts produced by data_pipeline.py / ocr_engine.py."
    ),
)

_HTTP_TIMEOUT = 25
_SAMPLE_ROWS = 10  # hard cap: schema-inspection never returns more than this


def _get_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    resp = requests.get(url, params=params, timeout=_HTTP_TIMEOUT, headers={
        "User-Agent": "mef-subnational-efficiency-mcp/1.0 (+local audit pipeline)"
    })
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Tool 1 — buscar_datasets
# ---------------------------------------------------------------------------
@mcp.tool()
def buscar_datasets(query: str, rows: int = 10) -> dict[str, Any]:
    """Search the open-data portal's CKAN catalog by keyword.

    Calls the native endpoint /api/3/action/package_search?q={query} and
    returns ONLY lightweight identifiers (id, title, organization, num
    resources) — never resource bodies — so the agent can pick a dataset
    without risking a multi-hundred-MB payload landing in context.
    """
    try:
        payload = _get_json(CKAN_PACKAGE_SEARCH, {"q": query, "rows": min(rows, 25)})
    except requests.RequestException as exc:
        log.warning("buscar_datasets failed for %r: %s", query, exc)
        return {"ok": False, "query": query, "error": str(exc), "results": []}

    results = []
    for pkg in payload.get("result", {}).get("results", []):
        results.append({
            "id": pkg.get("id"),
            "name": pkg.get("name"),
            "title": pkg.get("title"),
            "organization": (pkg.get("organization") or {}).get("title"),
            "num_resources": len(pkg.get("resources", [])),
            "metadata_modified": pkg.get("metadata_modified"),
        })
    return {"ok": True, "query": query, "count": len(results), "results": results}


# ---------------------------------------------------------------------------
# Tool 2 — obtener_detalle_dataset
# ---------------------------------------------------------------------------
@mcp.tool()
def obtener_detalle_dataset(dataset_id: str) -> dict[str, Any]:
    """Resolve a dataset id/slug to its direct resource download URLs + formats.

    This is the hand-off point to data_pipeline.py: the agent fetches *links
    and metadata* here, then a local script performs the actual download and
    aggregation outside the model's context.
    """
    try:
        payload = _get_json(CKAN_PACKAGE_SHOW, {"id": dataset_id})
    except requests.RequestException as exc:
        log.warning("obtener_detalle_dataset failed for %r: %s", dataset_id, exc)
        return {"ok": False, "dataset_id": dataset_id, "error": str(exc), "resources": []}

    pkg = payload.get("result", {})
    resources = [
        {
            "resource_id": r.get("id"),
            "name": r.get("name"),
            "format": r.get("format"),
            "url": r.get("url"),
            "size_bytes": r.get("size"),
            "datastore_active": r.get("datastore_active", False),
        }
        for r in pkg.get("resources", [])
    ]
    return {
        "ok": True,
        "dataset_id": dataset_id,
        "title": pkg.get("title"),
        "notes": (pkg.get("notes") or "")[:500],
        "resources": resources,
    }


# ---------------------------------------------------------------------------
# Tool 3 — descargar_documento_1964
# ---------------------------------------------------------------------------
@mcp.tool()
def descargar_documento_1964(source_url: str = HISTORICAL_SOURCE_URL) -> dict[str, Any]:
    """Download the 1964 historical fiscal PDF into data/raw_pdfs/.

    Idempotent: if the file already exists locally, it is reused. The actual
    OCR pass is delegated to procesar_ocr_paginas_1964 / src/ocr_engine.py —
    this tool's only job is "get the bytes onto disk safely".
    """
    if HISTORICAL_PDF_PATH.exists() and HISTORICAL_PDF_PATH.stat().st_size > 0:
        return {
            "ok": True,
            "path": str(HISTORICAL_PDF_PATH),
            "bytes": HISTORICAL_PDF_PATH.stat().st_size,
            "source": source_url,
            "cached": True,
        }
    try:
        resp = requests.get(source_url, timeout=60, headers={
            "User-Agent": "mef-subnational-efficiency-mcp/1.0 (+local audit pipeline)"
        })
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        if "pdf" not in content_type.lower() and not source_url.lower().endswith(".pdf"):
            return {
                "ok": False,
                "error": (
                    "URL did not return a PDF (got content-type "
                    f"'{content_type}'). Pass the direct .pdf resource URL "
                    "located on the Fuentes Históricas del Perú archive page."
                ),
                "source": source_url,
            }
        HISTORICAL_PDF_PATH.write_bytes(resp.content)
        return {
            "ok": True,
            "path": str(HISTORICAL_PDF_PATH),
            "bytes": len(resp.content),
            "source": source_url,
            "cached": False,
        }
    except requests.RequestException as exc:
        log.warning("descargar_documento_1964 failed: %s", exc)
        return {"ok": False, "error": str(exc), "source": source_url}


# ---------------------------------------------------------------------------
# Tool 4 — listar_entidades_publicas
# ---------------------------------------------------------------------------
@mcp.tool()
def listar_entidades_publicas(kind: str = "organization", limit: int = 25) -> dict[str, Any]:
    """List active public entities (ministries, regional governments, municipalities).

    `kind` maps to a CKAN facet/list action: 'organization' | 'group'.
    Returns names only — the agent resolves an entity to its budget records
    via consultar_datastore_filtrado, never by bulk export.
    """
    action = "organization_list" if kind == "organization" else "group_list"
    url = f"https://www.datosabiertos.gob.pe/api/3/action/{action}"
    try:
        payload = _get_json(url, {"all_fields": True, "limit": limit})
    except requests.RequestException as exc:
        log.warning("listar_entidades_publicas failed: %s", exc)
        return {"ok": False, "kind": kind, "error": str(exc), "entities": []}

    entities = [
        {"name": item.get("name"), "title": item.get("title"), "package_count": item.get("package_count")}
        for item in payload.get("result", [])[:limit]
    ]
    return {"ok": True, "kind": kind, "count": len(entities), "entities": entities}


# ---------------------------------------------------------------------------
# Tool 5 — listar_categorias_tematicas
# ---------------------------------------------------------------------------
@mcp.tool()
def listar_categorias_tematicas(limit: int = 20) -> dict[str, Any]:
    """Map high-level thematic groups (e.g. 'Economía y Finanzas', 'Salud')
    so the agent can scope buscar_datasets queries to the right domain instead
    of crawling the whole catalog."""
    url = "https://www.datosabiertos.gob.pe/api/3/action/group_list"
    try:
        payload = _get_json(url, {"all_fields": True, "limit": limit})
    except requests.RequestException as exc:
        log.warning("listar_categorias_tematicas failed: %s", exc)
        return {"ok": False, "error": str(exc), "categories": []}

    categories = [
        {"name": g.get("name"), "title": g.get("title"), "package_count": g.get("package_count")}
        for g in payload.get("result", [])[:limit]
    ]
    return {"ok": True, "count": len(categories), "categories": categories}


# ---------------------------------------------------------------------------
# Tool 6 — obtener_ultimas_actualizaciones
# ---------------------------------------------------------------------------
@mcp.tool()
def obtener_ultimas_actualizaciones(query: str = "presupuesto", rows: int = 10) -> dict[str, Any]:
    """Surface the most recently modified datasets matching `query`, sorted
    by metadata_modified desc — lets the agent detect that, e.g., a new
    2025-Q4 SIAF extract has landed without re-scanning the whole catalog."""
    try:
        payload = _get_json(
            CKAN_PACKAGE_SEARCH,
            {"q": query, "rows": min(rows, 25), "sort": "metadata_modified desc"},
        )
    except requests.RequestException as exc:
        log.warning("obtener_ultimas_actualizaciones failed: %s", exc)
        return {"ok": False, "error": str(exc), "updates": []}

    updates = [
        {"id": p.get("id"), "title": p.get("title"), "metadata_modified": p.get("metadata_modified")}
        for p in payload.get("result", {}).get("results", [])
    ]
    return {"ok": True, "query": query, "count": len(updates), "updates": updates}


# ---------------------------------------------------------------------------
# Tool 7 — inspeccionar_esquema_csv   (THE anti-flood safeguard)
# ---------------------------------------------------------------------------
@mcp.tool()
def inspeccionar_esquema_csv(resource_url: str, max_rows: int = _SAMPLE_ROWS) -> dict[str, Any]:
    """Stream-read ONLY the first `max_rows` (<=10) lines of a remote CSV to
    capture headers + sample rows — never the full body.

    This is the mandated "schema snapshot" tool: it opens a partial HTTP range
    stream, decodes a bounded chunk, and hands back column names, inferred
    dtypes and a tiny sample so the agent can plan a local aggregation script
    (data_pipeline.py) instead of loading the dataset into context.
    """
    import csv
    import io

    max_rows = min(max_rows, _SAMPLE_ROWS)
    try:
        with requests.get(
            resource_url,
            stream=True,
            timeout=_HTTP_TIMEOUT,
            headers={
                "User-Agent": "mef-subnational-efficiency-mcp/1.0 (+local audit pipeline)",
                # Ask politely for only the first ~64KB — enough for headers + sample rows
                "Range": "bytes=0-65535",
            },
        ) as resp:
            resp.raise_for_status()
            chunk = next(resp.iter_content(chunk_size=65536, decode_unicode=False))
    except (requests.RequestException, StopIteration) as exc:
        log.warning("inspeccionar_esquema_csv failed for %s: %s", resource_url, exc)
        return {"ok": False, "url": resource_url, "error": str(exc)}

    text = chunk.decode("utf-8", errors="replace")
    # Drop a possibly-truncated trailing line from the partial fetch
    lines = text.splitlines()[:-1] if len(text.splitlines()) > max_rows else text.splitlines()
    reader = csv.reader(lines)
    rows = list(reader)
    if not rows:
        return {"ok": False, "url": resource_url, "error": "empty/unreadable response"}

    header, sample = rows[0], rows[1 : max_rows + 1]
    return {
        "ok": True,
        "url": resource_url,
        "columns": header,
        "n_columns": len(header),
        "sample_rows": sample,
        "note": "Partial-range preview only (<=64KB). Full extraction must go through data_pipeline.py.",
    }


# ---------------------------------------------------------------------------
# Tool 8 — consultar_datastore_filtrado
# ---------------------------------------------------------------------------
@mcp.tool()
def consultar_datastore_filtrado(
    resource_id: str,
    filters: dict[str, Any] | None = None,
    q: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """SQL-like remote filtering through CKAN's native datastore_search API.

    Pushes WHERE-style filtering down to the server (filters / full-text q)
    and caps `limit` at 200 rows — the agent gets a *filtered slice*, never
    the raw table, and any heavier aggregation is delegated to
    data_pipeline.py via DuckDB against locally cached fragments.
    """
    params: dict[str, Any] = {"resource_id": resource_id, "limit": min(limit, 200)}
    if filters:
        params["filters"] = json.dumps(filters)
    if q:
        params["q"] = q
    try:
        payload = _get_json(CKAN_DATASTORE_SEARCH, params)
    except requests.RequestException as exc:
        log.warning("consultar_datastore_filtrado failed for %s: %s", resource_id, exc)
        return {"ok": False, "resource_id": resource_id, "error": str(exc), "records": []}

    result = payload.get("result", {})
    return {
        "ok": True,
        "resource_id": resource_id,
        "total": result.get("total"),
        "returned": len(result.get("records", [])),
        "fields": [f.get("id") for f in result.get("fields", [])],
        "records": result.get("records", []),
    }


# ---------------------------------------------------------------------------
# Tool 9 — procesar_ocr_paginas_1964
# ---------------------------------------------------------------------------
@mcp.tool()
def procesar_ocr_paginas_1964(
    pages: list[int] | None = None,
    n_pages: int = MIN_OCR_PAGES,
) -> dict[str, Any]:
    """Trigger the local PaddleOCR routine (src/ocr_engine.py) over >= 15
    distinct pages of the 1964 PDF.

    Returns a path to the microscopic extracted-text/structured-line Parquet
    in data/processed/ — raw OCR text never flows back through this tool, by
    design, to keep the agent's context lean.
    """
    from ocr_engine import run_ocr_pipeline  # local import: heavy deps loaded on demand

    n_pages = max(n_pages, MIN_OCR_PAGES)
    try:
        summary = run_ocr_pipeline(pages=pages, n_pages=n_pages)
    except Exception as exc:  # pragma: no cover - surfaced to the agent verbatim
        log.exception("procesar_ocr_paginas_1964 failed")
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **summary}


# ---------------------------------------------------------------------------
# Tool 10 — descargar_y_analizar_estadisticas
# ---------------------------------------------------------------------------
@mcp.tool()
def descargar_y_analizar_estadisticas(period: str, focus: str = "ejecucion") -> dict[str, Any]:
    """Run the local executor pipeline (src/data_pipeline.py) for `period`
    (e.g. '2025-12', '2025-Q4', '1964') and return a *descriptive-statistics*
    summary computed from the resulting microscopic snapshot — never the
    underlying rows.

    `focus` selects the metric family: 'ejecucion' (avance %, saldo no
    devengado) for 2025, or 'historico' for 1964 OCR-derived line counts.
    """
    from data_pipeline import run_period_pipeline

    try:
        parsed = parse_period(period)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        snapshot = run_period_pipeline(parsed, focus=focus)
    except Exception as exc:  # pragma: no cover
        log.exception("descargar_y_analizar_estadisticas failed")
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "period": parsed.label, "focus": focus, **snapshot}


# ---------------------------------------------------------------------------
# Resources — expose local artifact directories so Claude Code can discover
# what the pipeline already produced without re-running it blindly.
# ---------------------------------------------------------------------------
@mcp.resource("mef://snapshots")
def list_snapshots() -> str:
    """Lightweight directory listing of data/snapshots/ (schema captures)."""
    items = sorted(p.name for p in SNAPSHOT_DIR.glob("*") if p.is_file())
    return json.dumps({"snapshots": items}, ensure_ascii=False, indent=2)


@mcp.resource("mef://processed")
def list_processed() -> str:
    """Lightweight directory listing of data/processed/ (microscopic artifacts)."""
    items = sorted(p.name for p in PROCESSED_DIR.glob("*") if p.is_file())
    return json.dumps({"processed": items}, ensure_ascii=False, indent=2)


@mcp.resource("mef://raw_pdfs")
def list_raw_pdfs() -> str:
    """Lightweight directory listing of data/raw_pdfs/ (1964 source documents)."""
    items = sorted(p.name for p in RAW_PDF_DIR.glob("*") if p.is_file())
    return json.dumps({"raw_pdfs": items}, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    log.info("Starting local MCP server 'mef-subnational-efficiency' (stdio transport)")
    mcp.run(transport="stdio")
