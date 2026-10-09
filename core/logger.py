"""
core/logger.py
Logging estructurado usando structlog.
Escribe a stdout (con colores) y a log.txt (sin colores, rotación automática).
"""
from __future__ import annotations

import io
import logging
import logging.handlers
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Optional

import structlog

# Hook opcional inyectado por dashboard/server.py para reflejar cada evento
# en el buffer que consume la UI web, sin acoplar core.logger al dashboard.
_dashboard_sink: Optional[Callable[..., None]] = None

# Regex para eliminar secuencias ANSI del texto que va al fichero
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# CORREGIDO: en una vuelta anterior cambié esta ruta a una carpeta
# relativa al proyecto por "portabilidad", sin que se pidiera — pero el
# usuario ya dependía de tenerla en su Escritorio (la abre directamente
# desde ahí). Se restaura la ruta original como default; sigue siendo
# posible sobreescribirla con la variable de entorno BOT_LOG_FILE si se
# quiere mover a otra ubicación.
_LOG_FILE = os.environ.get("BOT_LOG_FILE", r"C:\Users\ema\Desktop\KrakenDirecto.txt")


def register_dashboard_sink(sink: Callable[..., None]) -> None:
    """Registra un callback que recibe (level, event, **kwargs) por cada log."""
    global _dashboard_sink
    _dashboard_sink = sink


def _dashboard_processor(logger: Any, method_name: str, event_dict: dict) -> dict:
    """Processor de structlog que reenvía el evento al sink del dashboard, si existe."""
    if _dashboard_sink is not None:
        try:
            payload = {k: v for k, v in event_dict.items() if k not in ("event", "level")}
            _dashboard_sink(
                event_dict.get("level", method_name),
                event_dict.get("event", ""),
                **payload,
            )
        except Exception:
            pass  # El dashboard nunca debe romper el logging del bot
    return event_dict


class _TeeWriter:
    """
    Escribe cada línea tanto a stdout como a un fichero (sin colores ANSI),
    con rotación real por tamaño.

    CORREGIDO: el docstring del módulo prometía "rotación automática" pero
    el código simplemente abría el fichero en modo append para siempre —
    sin ningún límite de tamaño ni backups. En una sesión larga (el bot
    corre 24/7) esto crece sin límite, hace cada vez más lenta la
    búsqueda/diagnóstico sobre el log, y puede llegar a llenar el disco.
    Ahora se rota manualmente cada _MAX_BYTES, conservando _BACKUP_COUNT
    ficheros antiguos (kraken.txt.1, kraken.txt.2, ...).
    """

    _MAX_BYTES = 20 * 1024 * 1024  # 20 MB por fichero
    _BACKUP_COUNT = 5

    def __init__(self, log_file_path: str) -> None:
        self._stdout = sys.stdout
        self._path = log_file_path
        self._file: Optional[io.TextIOWrapper] = None
        try:
            Path(log_file_path).parent.mkdir(parents=True, exist_ok=True)
            self._file = open(log_file_path, "a", encoding="utf-8", buffering=1)
        except Exception:
            self._file = None

    def _rotate_if_needed(self) -> None:
        if self._file is None:
            return
        try:
            if self._file.tell() < self._MAX_BYTES:
                return
        except Exception:
            return
        try:
            self._file.close()
            for i in range(self._BACKUP_COUNT - 1, 0, -1):
                src = f"{self._path}.{i}"
                dst = f"{self._path}.{i + 1}"
                if os.path.exists(src):
                    os.replace(src, dst)
            if os.path.exists(self._path):
                os.replace(self._path, f"{self._path}.1")
        except Exception:
            pass
        finally:
            try:
                self._file = open(self._path, "a", encoding="utf-8", buffering=1)
            except Exception:
                self._file = None

    def write(self, message: str) -> None:
        if message:
            self._stdout.write(message)
            if self._file is not None:
                try:
                    clean = _ANSI_RE.sub("", message)
                    self._file.write(clean)
                    self._file.flush()
                    self._rotate_if_needed()
                except Exception:
                    pass

    def flush(self) -> None:
        self._stdout.flush()
        if self._file is not None:
            try:
                self._file.flush()
            except Exception:
                pass


def configure_logging(level: str = "INFO", use_json: bool = True) -> None:
    """Configura structlog + logging estándar + log persistente a fichero."""
    tee = _TeeWriter(_LOG_FILE)

    logging.basicConfig(
        format="%(message)s",
        stream=tee,
        level=getattr(logging, level.upper(), logging.INFO),
        force=True,
    )
    for _noisy in ("uvicorn.access", "uvicorn.error", "httpx", "httpcore", "ccxt"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)

    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _dashboard_processor,
    ]

    if use_json:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(
            structlog.dev.ConsoleRenderer(
                colors=True,
                exception_formatter=structlog.dev.plain_traceback,
                level_styles={
                    "debug":    structlog.dev.ConsoleRenderer.get_default_level_styles()["debug"],
                    "info":     "\033[32m",      # verde
                    "warning":  "\033[33m",      # amarillo
                    "error":    "\033[31m",      # rojo
                    "critical": "\033[1m\033[35m", # magenta bold
                },
            )
        )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=tee),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.BoundLogger:
    return structlog.get_logger(name)
