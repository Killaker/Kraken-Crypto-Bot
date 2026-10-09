"""
pyi_rthook_syspath.py
Runtime hook de PyInstaller — colócalo en la raíz del proyecto.
Se ejecuta ANTES de cualquier import del script principal.
Inserta sys._MEIPASS en sys.path para que los paquetes locales
(core, engines, strategies, models, utils, dashboard) sean importables.
"""
import os
import sys

bundle_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
if bundle_dir not in sys.path:
    sys.path.insert(0, bundle_dir)
