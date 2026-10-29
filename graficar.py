"""Gráfico de barras con el porcentaje de aciertos por modo, a partir de resultados.csv.

    uv run graficar.py
"""
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

AQUI = Path(__file__).parent
ORDEN = ["memoria", "web", "mcp"]

res = pd.read_csv(AQUI / "resultados.csv")
modos = [m for m in ORDEN if m in set(res.modo)]
total = res.groupby("modo").size().reindex(modos)
aciertos = res[res.resultado == "acierta"].groupby("modo").size().reindex(modos, fill_value=0)
pct = aciertos / total * 100

fig, ax = plt.subplots(figsize=(7, 4.5))
barras = ax.bar(modos, pct, color="#3b6ea5", width=0.6)
for barra, n, t in zip(barras, aciertos, total):
    ax.text(barra.get_x() + barra.get_width() / 2, barra.get_height() + 2, f"{n}/{t}",
            ha="center", va="bottom", fontsize=12)

ax.set_ylim(0, 110)
ax.set_ylabel("Aciertos (%)")
ax.set_title("Aciertos por modo: memoria, búsqueda web y MCP del BCRP")
ax.spines[["top", "right"]].set_visible(False)
fig.tight_layout()
fig.savefig(AQUI / "grafico.png", dpi=150)
print("Guardado grafico.png:", dict(zip(modos, aciertos)))
