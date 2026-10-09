# kraken_bot_directo.spec
# Compilar con:  pyinstaller kraken_bot_directo.spec
#
# Coloca este archivo en la RAÍZ del proyecto (junto a main.py).
# También coloca pyi_rthook_syspath.py en la misma raíz.

import os
from PyInstaller.utils.hooks import collect_all, collect_data_files

datas = []
binaries = []
hiddenimports = []

# Paquetes externos estándar
EXT_PACKAGES = (
    "uvicorn",
    "fastapi",
    "starlette",
    "pydantic",
    "pydantic_settings",
    "anyio",
    "h11",
    "httptools",
    "websockets",
    "structlog",        
    "python_dotenv",    
    "dotenv",
)

for pkg in EXT_PACKAGES:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass  # paquete opcional no instalado, ignorar

# Recolectamos ccxt una sola vez fuera del bucle y excluimos el módulo problemático
try:
    d, b, h = collect_all("ccxt", exclusions=["ccxt.static_dependencies.starknet.serialization"])
    datas += d
    binaries += b
    hiddenimports += h
except Exception:
    pass

# Forzamos los submódulos de forma limpia (Sin espacios en blanco al inicio de la línea)
hiddenimports += [
    "ccxt.static_dependencies",
    "ccxt.static_dependencies.starknet",
    "ccxt.static_dependencies.starknet.abi",
    "ccxt.static_dependencies.starknet.serialization"
]

# ── Paquetes locales del proyecto (listado explícito) ────────────────────────
LOCAL_MODULES = [
    "core",
    "core.config",
    "core.exceptions",
    "core.logger",
    "engines",
    "engines.data_engine",
    "engines.execution_engine",
    "engines.risk_manager",
    "engines.strategy_engine",
	"engines.trend_watcher",
    "strategies",
    "strategies.base_strategy",
    "strategies.mean_reversion",
    "strategies.volatility_breakout",
    "strategies.synthetic_arb",
    "strategies.smart_routing",
    "strategies.portfolio_rebalance",
    "strategies.delta_neutral_arb",
    "strategies.cross_rate_pairs",
    "strategies.cross_currency_arb",
    "strategies.trend_momentum",
    "strategies.dip_rebound",
	
    "models",
    "models.market_data",
    "models.order",
    "models.signal",
    "utils",
    "utils.filters",
    "utils.indicators",
    "dashboard",
    "dashboard.server",
    "dashboard.config_manager",
]
hiddenimports += LOCAL_MODULES

# ── Archivos de datos ─────────────────────────────────────────────────────────
datas += [("dashboard/static", "dashboard/static")]

# Incluir paquetes locales como source .py
import os as _os
for _pkg in ("core", "engines", "strategies", "models", "utils", "dashboard"):
    if _os.path.isdir(_pkg):
        datas += [(_pkg, _pkg)]

if os.path.exists(".env"):
    datas += [(".env", ".")]

# ── Runtime hook ──────────────────────────────────────────────────────────────
RUNTIME_HOOKS = ["pyi_rthook_syspath.py"]

# ─────────────────────────────────────────────────────────────────────────────

a = Analysis(
    ["main.py"],
    pathex=["."],           
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=RUNTIME_HOOKS,
    excludes=[
        "tkinter", "matplotlib", "PIL", "PyQt5", "PySide2",
        "jupyter", "IPython", "notebook",
        "ccxt.static_dependencies.starknet.serialization"
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Kraken Arb. Bot",
    debug=False,            
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    icon="Kraken.ico",
)
