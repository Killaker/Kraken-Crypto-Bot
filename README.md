# Kraken-Crypto-Bot
Kraken crypto bot written in Python

INTRO
-------

- Bot automatizado escrito en Python para su uso en el exchange Kraken mediante uso de API, incluye 10 estrategias distintas tanto de Trading como de Arbitraje en SPOT.
- Utiliza BTC como activo base para la conversion entre los pares directos "SOL", "XRP", "ADA", "DOT", "LTC", "LINK", "ATOM", "BCH", "ETH", "DOGE", "ALGO".
- Lanza interfaz web local en http://127.0.0.1:8001 para monitorear precios en vivo de BTC y de todos los pares usados, posiciones abiertas, balance disponible, estrategias y monedas activas, drawdown diario y log.
- Botones para activar/desactivar tanto estrategias como monedas usadas, pausa/reanudacion de nuevas aperturas de posiciones, cerrar manualmente posiciones abiertas, reseteo de kill-switch al sobrepasar el drawdown diario.
- Marcadores en verde/rojo junto a las posiciones abiertas para indicar cuando reportan perdidas o ganancias netas teniendo en cuenta las comisiones y tasas por intercambio de Kraken.
- Notificacion mediante pop-up de cuando una moneda entra en una tendencia alcista/bajista.
- Todo el funcionamiento y monitoreo es completamente en tiempo real.

PREPARACION
--------------

- Instalar Python 3.10+
- Instalar pip para Python
- Clonar o descargar este repositorio
- Entra en la carpeta del raiz del repositorio descargado
- Editar ".env.example" con los datos de tu API creada en Kraken y renombrarlo a ".env"
- Instalar con pip el archivo "requirements.txt"

USO
--------

OPCION A(Lanzar):
- Lanzar el script "main.py"

OPCION B(Compilar .exe):
- Instalar modulo "pyinstaller" con pip
- Ejecutar "pyinstaller" usando el archivo "kraken_bot_directo.spec"
- Copiar el .exe creado en la carpeta "dist" a la raiz del proyecto junto al archivo ".env"
- Lanzar el .exe

ADVERTENCIA
---------------

- Por defecto el bot se conecta en modo "PAPER" para hacer pruebas con dinero ficticio/virtual con un balance de 0.03 BTC, para usar tu saldo real de Kraken cambiar en el archivo ".env" la variable "BOT_MODE=PAPER" a "BOT_MODE=LIVE"