"""Servidor MCP del BCRP. Empiecen aquí: buscar_series ya funciona; completen los TODO hasta que pasen las pruebas.

    uv run pytest                                           # el contrato: las 8 pruebas deben pasar
    uv run server.py                                        # el servidor, por stdio
    claude mcp add --scope project bcrp -- uv run server.py # conectarlo a Claude Code

Las cuentas las hace Python, no el modelo: así cada cifra sale de una llamada que se puede repetir.
"""
import re
import time
import unicodedata
from functools import cache
from pathlib import Path

import httpx
import pandas as pd
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

AQUI = Path(__file__).parent
CATALOGO = AQUI / "datos" / "catalogo_bcrp.csv"
URL_CATALOGO = "https://estadisticas.bcrp.gob.pe/estadisticas/series/metadata"
API = "https://estadisticas.bcrp.gob.pe/estadisticas/series/api"
SOLO_LECTURA = {"readOnlyHint": True, "openWorldHint": True}

mcp = FastMCP("bcrp", instructions=(
    "Series oficiales del Banco Central de Reserva del Perú (BCRP). Primero busca el código con buscar_series; "
    "luego usa resumen para variaciones, promedios y extremos, y serie para los datos. Usa promedio_anual para "
    "promedios anuales o para comparar años, y no compares años incompletos (completo=False). No calcules a mano: "
    "cita siempre el código de la serie y el periodo."))


def _sin_tildes(s):
    return unicodedata.normalize("NFD", str(s).lower()).encode("ascii", "ignore").decode()


@cache
def catalogo():
    """El catálogo oficial: 17 mil series en unos 7 MB. Se baja una vez y queda en datos/."""
    if not CATALOGO.exists():
        CATALOGO.parent.mkdir(exist_ok=True)
        r = httpx.get(URL_CATALOGO, timeout=120)
        r.raise_for_status()
        CATALOGO.write_text(r.content.decode("latin-1"), encoding="utf-8")
    df = pd.read_csv(CATALOGO, sep=";", dtype=str).fillna("")
    df = df.rename(columns={"Código de serie": "codigo", "Categoría de serie": "categoria", "Grupo de serie": "grupo",
                            "Nombre de serie": "nombre", "Frecuencia": "frecuencia",
                            "Fecha de actualización": "actualizada", "Fecha de inicio": "inicio"})
    df["texto"] = (df.categoria + " " + df.grupo + " " + df.nombre).map(_sin_tildes)
    return df


def _ficha(codigo):
    fila = catalogo().query("codigo == @codigo")
    if fila.empty:
        raise ToolError(f"No existe el código de serie {codigo!r}. Búsquenlo con buscar_series.")
    f = fila.iloc[0]
    return {"codigo": f.codigo, "nombre": f"{f.grupo} - {f.nombre}", "categoria": f.categoria,
            "frecuencia": f.frecuencia, "inicio": f.inicio, "actualizada": f.actualizada}


@mcp.tool(annotations=SOLO_LECTURA)
def buscar_series(texto: str, frecuencia: str | None = None, limite: int = 10) -> list[dict]:
    """Busca series del BCRP por palabras: todas deben aparecer en la categoría, el grupo o el nombre.

    Ejemplos: "ipc 12 meses", "tipo de cambio interbancario venta", "reservas internacionales netas",
    "tasa de referencia", "cobre precio". frecuencia: "Diaria", "Mensual", "Trimestral" o "Anual".
    Devuelve el código, el nombre completo, la frecuencia y desde cuándo hay datos. Si no hay resultados,
    prueben con menos palabras.
    """
    df = catalogo()
    m = pd.Series(True, index=df.index)
    for palabra in _sin_tildes(texto).split():
        m &= df.texto.str.contains(re.escape(palabra))
    if frecuencia:
        m &= df.frecuencia.str.lower() == frecuencia.lower()
    # primero las series vigentes y, entre ellas, las de nombre más corto (las más generales)
    sel = df[m]
    orden = sel.assign(vieja=sel.actualizada < "2025", largo=(sel.grupo + sel.nombre).str.len())
    return [_ficha(c) for c in orden.sort_values(["vieja", "largo"]).head(limite).codigo]


# ---------------------------------------------------------------------------------------------------------------
# TODO 1 · serie
# ---------------------------------------------------------------------------------------------------------------
MESES = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
         "jul": 7, "ago": 8, "set": 9, "sep": 9, "oct": 10, "nov": 11, "dic": 12}


def _tipo(frecuencia):
    """'Mensual' -> 'mensual'. Así comparamos sin importar mayúsculas ni tildes."""
    return _sin_tildes(frecuencia).strip()


def _fecha_api(fecha, frecuencia):
    """Convierte la fecha que da el usuario al formato que pide la API.

    mensual:    "2025-01" -> "2025-1"      trimestral: "2025-T1" o "2025-1" -> "2025-1"
    diaria:     "2026-10-01" (igual)       anual:      "2025" o "2025-01" -> "2025"
    """
    tipo = _tipo(frecuencia)
    partes = re.findall(r"\d+", str(fecha))          # "2025-T1" -> ["2025", "1"]
    if not partes or len(partes[0]) != 4:
        raise ToolError(f"Fecha {fecha!r} no válida. Usen \"2025-01\" (mensual), \"2025-01-15\" (diaria), "
                        f"\"2025-T1\" (trimestral) o \"2025\" (anual).")
    anio = partes[0]
    if tipo == "anual":
        return anio
    if tipo == "diaria":
        if len(partes) < 3:
            raise ToolError(f"La serie es diaria: la fecha {fecha!r} debe ser como \"2026-10-15\".")
        return f"{anio}-{int(partes[1]):02d}-{int(partes[2]):02d}"
    if len(partes) < 2:
        raise ToolError(f"La serie es {tipo}: la fecha {fecha!r} debe incluir mes o trimestre, p. ej. \"2025-01\".")
    return f"{anio}-{int(partes[1])}"                # mensual y trimestral: sin cero adelante


def _anio(texto):
    """'26' -> 2026, '2026' -> 2026."""
    return int(texto) + 2000 if len(texto) == 2 else int(texto)


def _periodo(nombre, frecuencia):
    """Convierte el nombre del periodo que devuelve la API a un formato estándar.

    "Ene.2025" -> "2025-01"   "01.Oct.26" -> "2026-10-01"   "T1.25" -> "2025-T1"   "2025" -> "2025"
    """
    tipo = _tipo(frecuencia)
    p = nombre.strip().split(".")
    if tipo == "mensual":                            # ["Ene", "2025"]
        return f"{_anio(p[1])}-{MESES[p[0][:3].lower()]:02d}"
    if tipo == "diaria":                             # ["01", "Oct", "26"]
        return f"{_anio(p[2])}-{MESES[p[1][:3].lower()]:02d}-{int(p[0]):02d}"
    if tipo == "trimestral":                         # ["T1", "25"]
        return f"{_anio(p[1])}-{p[0].upper()}"
    return p[0]                                      # anual: "2025"


@cache
def _descargar(url):
    """Baja el JSON de la API. Si responde HTML (demasiados pedidos), espera y reintenta.
    Con @cache, cada URL se baja una sola vez por sesión (los errores no se guardan)."""
    for intento in range(5):
        try:
            r = httpx.get(url, timeout=60, follow_redirects=True)
            r.raise_for_status()
            return r.json()
        except (ValueError, httpx.HTTPError):        # ValueError: llegó HTML en vez de JSON
            time.sleep(2 * (intento + 1))            # 2, 4, 6, 8, 10 segundos
    raise ToolError(f"El BCRP no respondió con datos para {url}. Esperen unos segundos y reintenten.")


@mcp.tool(annotations=SOLO_LECTURA)
def serie(codigo: str, desde: str, hasta: str) -> dict:
    """Datos de una serie del BCRP entre dos fechas, ambas incluidas.

    Úsenla cuando necesiten los valores periodo por periodo (por ejemplo, el dato de un mes o día concreto).
    Para variaciones, promedios o extremos usen resumen. Fechas según la frecuencia de la serie:
    "2025-01" (mensual), "2025-01-15" (diaria), "2025-T1" (trimestral), "2025" (anual).
    Ejemplo: serie("PN01273PM", "2025-01", "2025-12"). Devuelve los datos y la URL de la fuente.
    """
    ficha = _ficha(codigo)                           # ToolError si el código no existe
    d, h = _fecha_api(desde, ficha["frecuencia"]), _fecha_api(hasta, ficha["frecuencia"])
    fuente = f"{API}/{codigo}/json/{d}/{h}"
    respuesta = _descargar(fuente)
    datos = []
    for p in respuesta.get("periods", []):
        valor = p["values"][0]
        if valor.strip().lower() in ("n.d.", "n.d", ""):
            continue                                 # periodo sin dato
        datos.append({"periodo": _periodo(p["name"], ficha["frecuencia"]), "valor": float(valor)})
    return {"codigo": codigo, "nombre": ficha["nombre"], "frecuencia": ficha["frecuencia"],
            "datos": datos, "fuente": fuente}


# ---------------------------------------------------------------------------------------------------------------
# TODO 2 · resumen
# ---------------------------------------------------------------------------------------------------------------
@mcp.tool(annotations=SOLO_LECTURA)
def resumen(codigo: str, desde: str, hasta: str) -> dict:
    """Resumen de una serie del BCRP entre dos fechas: primer y último dato, cambio absoluto, variación %,
    promedio, máximo y mínimo (con su periodo).

    Úsenla siempre que la pregunta pida comparar, promediar o encontrar extremos: así no se calcula a mano.
    Mismos formatos de fecha que serie. Ejemplo: resumen("PN01273PM", "2025-12", "2026-09").
    """
    s = serie(codigo, desde, hasta)
    datos = s["datos"]
    if not datos:
        raise ToolError(f"La serie {codigo} no tiene datos entre {desde} y {hasta}.")
    primero, ultimo = datos[0], datos[-1]
    maximo = max(datos, key=lambda x: x["valor"])
    minimo = min(datos, key=lambda x: x["valor"])
    cambio = ultimo["valor"] - primero["valor"]
    return {
        "codigo": codigo, "nombre": s["nombre"], "frecuencia": s["frecuencia"], "n": len(datos),
        "primero": primero, "ultimo": ultimo,
        "cambio": round(cambio, 4),
        "variacion_pct": round(100 * cambio / primero["valor"], 4) if primero["valor"] else None,
        "promedio": round(sum(x["valor"] for x in datos) / len(datos), 4),
        "maximo": maximo, "minimo": minimo,
        "fuente": s["fuente"],
    }


# ---------------------------------------------------------------------------------------------------------------
# TODO 3 · herramienta propia: promedio anual de una serie mensual
# ---------------------------------------------------------------------------------------------------------------
def _num(x):
    """Redondea a 4 decimales; NaN o infinito (no hay año anterior, o se dividió entre 0) -> None (null en JSON)."""
    return round(float(x), 4) if pd.notna(x) and abs(x) != float("inf") else None


@mcp.tool(annotations=SOLO_LECTURA)
def promedio_anual(codigo: str, desde: str, hasta: str) -> dict:
    """Promedio por año de una serie MENSUAL del BCRP, con el cierre de cada año y el cambio frente al anterior.

    Úsenla cuando pregunten por promedios anuales o pidan comparar años, por ejemplo «¿qué año tuvo la
    inflación promedio más alta?» o «¿cuánto subieron las reservas en promedio de 2024 a 2025?».
    desde y hasta son años. Ejemplo: promedio_anual("PN01273PM", "2016", "2026").

    Por cada año devuelve: promedio, cierre (último mes con dato), meses (cuántos tienen dato),
    completo (True si son 12), cambio_promedio y variacion_pct_promedio. Cuál usar:
    - para tasas, como la inflación o una tasa de interés: cambio_promedio, en puntos porcentuales;
    - para montos, como reservas o exportaciones: variacion_pct_promedio, en %.
    Los dos son null en el primer año del rango, porque no hay año anterior con qué comparar.
    Los años con completo=False (por ejemplo el año en curso) no son comparables con los completos:
    adviértanlo al responder. Devuelve también la URL de la fuente.
    """
    ficha = _ficha(codigo)                           # ToolError si el código no existe
    if _tipo(ficha["frecuencia"]) != "mensual":
        raise ToolError(f"La serie {codigo} es {ficha['frecuencia'].lower()}: promedio_anual solo acepta series "
                        "mensuales. Busquen la versión mensual con buscar_series(..., frecuencia=\"Mensual\").")
    for anio in (desde, hasta):
        if not re.fullmatch(r"\d{4}", str(anio)):
            raise ToolError(f"{anio!r} no es un año: usen cuatro dígitos, por ejemplo \"2016\".")

    s = serie(codigo, f"{desde}-01", f"{hasta}-12")
    if not s["datos"]:
        raise ToolError(f"La serie {codigo} no tiene datos entre {desde} y {hasta}.")

    df = pd.DataFrame(s["datos"])                    # columnas: periodo ("2016-01") y valor
    df["anio"] = df.periodo.str[:4].astype(int)      # "2016-01" -> 2016
    g = df.groupby("anio").valor.agg(promedio="mean", cierre="last", meses="count")
    # Si falta un año entero, lo agregamos vacío para que diff() no compare 2018 contra 2016.
    g = g.reindex(range(g.index.min(), g.index.max() + 1))
    g["cambio_promedio"] = g.promedio.diff()                     # puntos: promedio de este año - del anterior
    g["variacion_pct_promedio"] = g.promedio.pct_change() * 100  # %: (este / anterior - 1) * 100
    g = g.dropna(subset=["promedio"])                # quitamos los años vacíos que agregamos

    anios = [{"anio": str(anio), "promedio": _num(f.promedio), "cierre": _num(f.cierre),
              "meses": int(f.meses), "completo": int(f.meses) == 12,
              "cambio_promedio": _num(f.cambio_promedio),
              "variacion_pct_promedio": _num(f.variacion_pct_promedio)}
             for anio, f in g.iterrows()]
    return {"codigo": codigo, "nombre": s["nombre"], "anios": anios, "fuente": s["fuente"]}


# ---------------------------------------------------------------------------------------------------------------
# TODO 4 · recurso: la ficha de una serie
# ---------------------------------------------------------------------------------------------------------------
@mcp.resource("bcrp://serie/{codigo}")
def ficha(codigo: str) -> dict:
    """Ficha de una serie del BCRP: nombre, categoría, frecuencia, fecha de inicio y última actualización."""
    return _ficha(codigo)


# Recursos fijos para las series más usadas: a diferencia de la plantilla, el cliente los lista y los adjunta
# directamente desde el menú @. La fábrica fija el código de cada vuelta (sin parámetros: si no, sería plantilla).
SERIES_FIJAS = ["PN01273PM", "PN01271PM", "PD04638PD", "PD04722MM"]


def _lector(codigo):
    def leer() -> dict:
        return _ficha(codigo)
    return leer


for _codigo in SERIES_FIJAS:
    mcp.resource(f"bcrp://serie/{_codigo}", name=f"serie_{_codigo}",
                 description=f"Ficha de la serie {_codigo} del BCRP.", mime_type="application/json")(_lector(_codigo))


# ---------------------------------------------------------------------------------------------------------------
# TODO 5 · prompt: informe con cifras verificables
# ---------------------------------------------------------------------------------------------------------------
@mcp.prompt
def informe(tema: str) -> str:
    """Informe breve sobre un tema económico del Perú con cifras verificables del BCRP."""
    return (f"Escribe 5 viñetas sobre «{tema}» en el Perú con datos oficiales del BCRP. "
            "Primero busca las series con buscar_series; usa resumen para variaciones y extremos, y serie "
            "para datos puntuales. No calcules a mano. Cada viñeta debe citar el código de la serie, "
            "el periodo y la URL de la fuente.")


if __name__ == "__main__":
    mcp.run()
