# mef_subnational_efficiency_mcp

**Auditoría del gasto público mediante sistemas multiagente, habilidades de Claude Code y MCP local.**

Pipeline local, de nivel de producción, que conecta Claude Code (CLI) — vía un servidor MCP propio — con el
[Portal de Datos Abiertos del Perú](https://www.datosabiertos.gob.pe) para auditar la ejecución del
presupuesto público subnacional del MEF/SIAF en el año fiscal 2025, **y** una pista independiente que usa
PaddleOCR para digitalizar el archivo histórico de 1964 *"Presupuesto, Balance y Cuenta General de la
República"* (Ministerio de Hacienda y Comercio). Dos habilidades cooperativas — **Executor** y
**Evaluator/Optimizer** — construyen conjuntamente un dashboard Streamlit de 4 pestañas.

---

## 1. Arquitectura general

```
                ┌──────────────────────────┐
                │   Portal de Datos         │
 datosabiertos. │   Abiertos del Perú       │
   gob.pe   ───▶│   (CKAN API + datastore)  │
                └────────────┬─────────────┘
                             │  schema/sample previews ONLY (≤10 rows)
                             ▼
   ┌─────────────────────────────────────────────────────┐
   │      src/mcp_server.py  (Local MCP server, stdio)     │
   │  buscar_datasets · obtener_detalle_dataset            │
   │  inspeccionar_esquema_csv · consultar_datastore_*     │
   │  descargar_documento_1964 · procesar_ocr_paginas_1964 │
   │  descargar_y_analizar_estadisticas · …                │
   └───────────────┬───────────────────────┬──────────────┘
                   │                        │
        triggers   ▼                        ▼   triggers
   ┌────────────────────────┐   ┌─────────────────────────┐
   │ src/data_pipeline.py    │   │ src/ocr_engine.py        │
   │ (pandas, isolated, run  │   │ (PaddleOCR, ≥15 pages    │
   │  OUTSIDE the LLM ctx)   │   │  of the 1964 PDF)        │
   └───────────┬────────────┘   └────────────┬────────────┘
               │  microscopic Parquet/JSON    │
               ▼                              ▼
            data/processed/  ◀── only this directory is ever read by app.py
                              │
        ┌─────────────────────┴─────────────────────┐
        │   .claude/skills/executor_skill.json        │  →  drafts app.py,
        │   .claude/skills/evaluator_skill.json       │  →  audits + optimizes it
        └─────────────────────┬─────────────────────┘
                              ▼
                         app.py  (Streamlit, 4 tabs)
```

### Por qué esta arquitectura es "anti-saturación de contexto"
El portal expone extractos CSV/JSON de 200 MB–1 GB. **Ningún componente de este repositorio
carga esos archivos completos en la ventana de contexto de Claude.** El flujo obligatorio es:

1. `inspeccionar_esquema_csv` abre un *stream* parcial (rango de 64 KB) y devuelve solo
   encabezados + una muestra de ≤ 10 filas → persistida en `data/snapshots/`.
2. Esa vista guía la escritura de un script **local y aislado** (`data_pipeline.py`,
   pandas/`to_parquet`) que descarga, filtra, agrega y escribe una **huella microscópica**
   (~100 filas, *Parquet*) en `data/processed/`.
3. `app.py` y las herramientas MCP de "estadísticas" (`descargar_y_analizar_estadisticas`)
   **solo leen ese fragmento reducido** — nunca el extracto crudo.

> Nota de transparencia: durante el desarrollo, el endpoint público
> `datosabiertos.gob.pe/api/3/action/package_search` respondió **HTTP 418** (bloqueo
> perimetral / WAF) desde el entorno de build. `data_pipeline.py` detecta esto y activa,
> de forma honesta y trazable, un **generador sintético sembrado y determinista**
> (`seed=20250101`, esquema idéntico al de "Consulta Amigable" del MEF: pliego, unidad
> ejecutora, departamento, sector, PIM, Devengado). Cada artefacto incluye el campo
> `source: "live_datastore" | "synthetic_fallback"` para que cualquier auditor distinga el
> origen de un vistazo. En una red sin ese bloqueo, `_try_live_datastore` toma precedencia
> automáticamente — no se requiere ningún cambio de código.

---

## 2. Estructura del repositorio

```
mef_subnational_efficiency_mcp/
├── app.py                          # Dashboard Streamlit de 4 pestañas
├── README.md                       # Este documento
├── requirements.txt                # Dependencias explícitas (incluye paddleocr)
├── .mcp.json                       # Registro del servidor MCP local para Claude Code
│
├── .claude/skills/
│   ├── executor_skill.json         # Motor de ingeniería de datos + composición de la app
│   └── evaluator_skill.json        # Motor de validación estructural, UX y QA
│
├── src/
│   ├── mcp_server.py               # Servidor MCP local — expone 10 herramientas + 3 recursos
│   ├── data_pipeline.py            # Workers locales aislados (pandas) — pista 2025
│   ├── ocr_engine.py               # Motor PaddleOCR (≥15 páginas del PDF de 1964)
│   ├── analytical_engine.py        # Métricas y agrupaciones (independientes por época)
│   └── utils.py                    # Rutas, logging, parseo de periodos, helpers seguros
│
├── data/
│   ├── raw_pdfs/                   # PDF de 1964 descargado (gitignored — se regenera)
│   ├── snapshots/                  # Esquemas + muestras top-10 (auditoría de procedencia)
│   └── processed/                  # Fragmentos limpios 2025 + dataframes OCR de 1964
│
└── video/
    └── link.txt                    # URL del video de presentación (≤ 5 min)
```

---

## 3. Instalación y ejecución

```bash
# 1. Entorno
python -m venv .venv && source .venv/bin/activate   # (Windows: .venv\Scripts\activate)
pip install -r requirements.txt

# 2. (Opcional) generar/actualizar fragmentos manualmente
python src/data_pipeline.py 2025-12      # pista moderna — escribe data/processed/exec_2025_2025-12.*
python src/ocr_engine.py                 # pista histórica — procesa ≥15 páginas del PDF de 1964

# 3. Lanzar el dashboard
streamlit run app.py
```

### Conectar el servidor MCP a Claude Code
El archivo `.mcp.json` ya registra `mef-subnational-efficiency` (transporte `stdio`,
`python src/mcp_server.py`). Claude Code lo detecta automáticamente al abrir el repositorio;
no requiere autenticación privilegiada — todas las llamadas usan los endpoints públicos de
CKAN del portal (lectura anónima).

---

## 4. Kit de herramientas MCP (`src/mcp_server.py`)

| Herramienta | Responsabilidad aislada |
|---|---|
| `buscar_datasets` | Busca en el catálogo CKAN por palabra clave (`package_search`) — devuelve solo ids/títulos/orgs |
| `obtener_detalle_dataset` | Resuelve un dataset a sus URLs de descarga directa y formatos (`package_show`) |
| `descargar_documento_1964` | Descarga (idempotente) el PDF histórico a `data/raw_pdfs/` |
| `listar_entidades_publicas` | Lista ministerios / gobiernos regionales / municipalidades activas |
| `listar_categorias_tematicas` | Mapea grupos temáticos de alto nivel del catálogo |
| `obtener_ultimas_actualizaciones` | Detecta datasets recientemente modificados (orden por `metadata_modified`) |
| `inspeccionar_esquema_csv` | **Guardia anti-flood**: stream parcial (64 KB) → encabezados + ≤10 filas de muestra |
| `consultar_datastore_filtrado` | Filtrado remoto tipo SQL vía `datastore_search` (cap. 200 filas, *server-side* `filters`/`q`) |
| `procesar_ocr_paginas_1964` | Dispara `ocr_engine.run_ocr_pipeline` (PaddleOCR, ≥15 páginas) — devuelve solo el resumen |
| `descargar_y_analizar_estadisticas` | Dispara `data_pipeline.run_period_pipeline(period)` — devuelve solo agregados descriptivos |

**Criterio de aislamiento de responsabilidades:** cada herramienta hace *una* cosa observable
desde el catálogo CKAN (buscar, detallar, listar, inspeccionar, filtrar, descargar) **o**
dispara *un* worker local pesado (OCR, agregación) — nunca ambas. Esto permite auditar qué
herramienta tocó la red pública vs. cuál solo orquestó cómputo local, y mantiene cada función
< 60 líneas y testeable de forma independiente. Se añadieron 3 *resources* de solo-lectura
(`mef://snapshots`, `mef://processed`, `mef://raw_pdfs`) para que el agente pueda *descubrir*
artefactos ya producidos sin re-disparar un worker innecesariamente.

---

## 5. Las dos habilidades cooperativas

### 🧠 `executor_skill` — Ingeniería de datos y motor de producción
Lee esquemas vía MCP, dispara `data_pipeline.py` / `ocr_engine.py`, construye los gráficos
estadísticos independientes de ambas épocas y escribe el borrador base de `app.py`.
Se invoca dinámicamente por periodo:

```bash
claude "run executor_skill for period 2025-12"
claude "execute mef_update for 2025-Q4"
claude "run executor_skill for period 1964 --track historical"
```

El periodo **nunca está hard-codeado**: `src/utils.parse_period` traduce `YYYY` / `YYYY-MM` /
`YYYY-QN` a un objeto `Period`, que fluye sin transformaciones hasta
`data_pipeline.run_period_pipeline`. La Tab 4 del dashboard expone un *sandbox* que invoca
exactamente la misma función — el ciclo CLI ↔ UI queda cerrado.

### ⚖️ `evaluator_skill` — Auditor Senior y UX Master
No solo emite crítica en texto: **ejecuta** sus propias auditorías y **modifica** el código.

1. **Auditoría de inconsistencia de datos** — re-muestrea las fuentes crudas vía MCP de forma
   independiente y recalcula los agregados del Executor (tolerancia: redondeo).
2. **Optimización de rendimiento** — fuerza `@st.cache_data` en cada función de carga/cómputo
   pesado de `app.py`, apuntando a renders < 1 s.
3. **Perfeccionamiento UX** — inyecta CSS, reemplaza divisiones directas por `utils.safe_div`
   (cero `ZeroDivisionError`/`inf` en producción), corrige configuración de ejes/escalas/colores.
4. **Informe de calidad** — escribe `data/processed/evaluator_report_<periodo>.md`, que la
   Tab 4 renderiza *verbatim* como bitácora de auditoría multiagente.

Ver un ciclo completo ya ejecutado en
[`data/processed/evaluator_report_2025-12.md`](data/processed/evaluator_report_2025-12.md).

---

## 6. Marco de métricas analíticas (`src/analytical_engine.py`)

### Pista moderna — FY2025 (PEN, MEF/SIAF)
- **Tasa de ejecución (Avance %)** = `(Devengado / PIM) × 100`
- **Saldo No Devengado ("capital congelado")** = `PIM − Devengado`
- Agregaciones por departamento (mapa + heatmap de estancamiento × vulnerabilidad social),
  ranking "Salón de la Vergüenza" (PIM > S/ 10M, ordenado por menor avance) y desglose por
  categoría de gasto bloqueada.

### Pista histórica — Archivo de 1964 (estrictamente descriptiva, sin PEN)
- Páginas analizadas, líneas capturadas, partidas cuantificadas vs. encabezados estructurales,
  participación cuantificada (%), promedio de líneas por página.
- **Sin fórmulas de comparación entre épocas**: los marcos contables de 1964 (rubros
  funcionales: instrucción pública, obras públicas, fuerzas armadas, deuda…) y de 2025
  (clasificadores PIM/Devengado) son estructuralmente incompatibles — el sistema las trata
  como registros analíticos completamente independientes, tal como exige el enunciado.

---

## 7. Pista PaddleOCR — Archivo de 1964

`src/ocr_engine.py` cumple la **regla de volumen** (≥ 15 páginas distintas) sobre
*"Presupuesto, Balance y Cuenta General de la República"* (Ministerio de Hacienda y Comercio,
1964; fuente: [Fuentes Históricas del Perú](https://fuenteshistoricasdelperu.com/2021/08/12/ministerio-de-hacienda-y-comercio-presupuesto-balance-y-cuenta-general-de-la-republica/)).

**Diseño orientado a memoria constante** (independiente de la longitud del documento):
- Las páginas se rasterizan **una a la vez** (`pdf2image.convert_from_path(first_page=last_page=N,
  thread_count=1)`) — nunca se mantiene el documento completo en memoria.
- El motor `PaddleOCR` se instancia **una sola vez** (singleton perezoso a nivel de módulo,
  `lang="es"`, `use_angle_cls=True`, `det_limit_side_len=1536`) y se reutiliza en todas las
  páginas — re-instanciar por página es la causa #1 del crecimiento de memoria reportado por
  usuarios de PaddleOCR en lotes largos.
- Pre-procesamiento ligero por página (escala de grises + umbral adaptativo de OpenCV) compensa
  el bajo contraste de los escaneos/microfilm de los años 60 sin requerir un modelo de detección
  más pesado.
- Caché a nivel de línea (`data/processed/ocr_lines_1964.parquet`): re-ejecuciones sobre el
  mismo conjunto de páginas son no-ops — la habilidad puede invocarse repetidamente en
  desarrollo sin re-pagar el costo de OCR.
- El texto OCR crudo **nunca** vuelve a fluir a través de la capa MCP hacia el contexto del
  modelo — solo viajan resúmenes (`pages_processed`, `meets_volume_rule`,
  `quantified_line_items`, ruta al Parquet).

Si el stack pesado (PaddleOCR/Poppler) o el PDF no están disponibles en el entorno de
ejecución, el módulo recurre a una **transcripción offline determinista y explícitamente
etiquetada** (`source = "offline_transcription"`) que respeta el mismo contrato de salida —
así el resto de la canalización (métricas, gráficos, Tab 1) sigue siendo demostrable de
extremo a extremo sin nunca disfrazar el origen del dato.

---

## 8. Especificación del dashboard (`app.py`, 4 pestañas)

1. **📊 Resumen Macro · Doble Época** — KPIs `st.metric` de 2025 (PIM, Devengado, Avance %,
   saldo no devengado) + narrativa del asesor de IA, **y** un contenedor histórico
   independiente con conclusiones en texto plano + **2 gráficos** (líneas OCR por página;
   distribución cuantificado vs. estructural) derivados de las ≥ 15 páginas analizadas.
   *Sin fórmulas de comparación entre épocas.*
2. **🗺️ Distribución Territorial 2025** — mapa interactivo (`plotly.scatter_mapbox`) y mapa
   de calor estancamiento × vulnerabilidad social. Exclusivamente datos 2025.
3. **🏛️ Salón de la Vergüenza · Anomalías 2025** — `st.dataframe` ordenable de unidades
   ejecutoras con PIM > S/ 10M y peor avance, + desglose visual de categorías de gasto
   bloqueadas (infraestructura de concreto, maquinaria, consultorías…). Exclusivamente 2025.
4. **🧪 Auditoría Multiagente & Sandbox 2025** — bitácora cruda del informe del
   Evaluator/Optimizer (Executor → build verificado) + sandbox interactivo que re-ejecuta
   `data_pipeline.run_period_pipeline` para cualquier periodo tecleado, replicando
   `claude "run executor_skill for period <periodo>"` sin reiniciar el proceso.

Toda función de carga/cómputo está decorada con `@st.cache_data`; todas las divisiones pasan
por `utils.safe_div`. Verificado en vivo (capturas tomadas con Playwright durante el
desarrollo): las 4 pestañas renderizan sin excepciones y en < 1 s con caché caliente.

---

## 9. Flujo de trabajo Git (obligatorio)

- ❌ Nunca se confirma directamente sobre `main`.
- ✅ Desarrollo en ramas aisladas por funcionalidad:
  `feature/mcp-server-core`, `feature/data-snapshot-pipeline`,
  `feature/historical-1964-paddle-ocr`, `feature/executor-dashboard-draft`,
  `feature/evaluator-qa-refinement`.
- ✅ Commits explícitos, paso a paso.
- ✅ Integración a `main` exclusivamente vía Pull Requests descriptivos.

---

## 10. Limitaciones conocidas y próximos pasos

- El acceso en vivo a `datosabiertos.gob.pe` puede estar sujeto a bloqueos perimetrales (WAF)
  según la red de origen — el *fallback* sintético sembrado garantiza una demo reproducible,
  pero la auditoría de producción requiere ejecutarse desde una red sin ese bloqueo (o con una
  llave/whitelist proporcionada por el portal).
- El *fallback* offline de OCR usa una transcripción determinista cuando PaddleOCR/Poppler no
  están instalados; en un entorno con el stack completo (`requirements.txt`), el motor real
  procesa las páginas reales del PDF descargado.
- Próximos pasos sugeridos: (a) cachear los recursos `datastore_active=true` descubiertos para
  evitar reconsultar `package_search` en cada periodo; (b) ampliar `DEPARTMENT_COORDS` con
  geometrías reales (GeoJSON del INEI) para choropletas de mayor fidelidad; (c) extender
  `evaluator_skill` con pruebas de regresión automatizadas (`pytest`) sobre
  `analytical_engine` para que el "romper cosas a propósito" quede versionado.
