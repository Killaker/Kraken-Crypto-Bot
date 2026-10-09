@echo off
:: Configura la codificación de caracteres para permitir acentos y eñes
chcp 65001 > nul

echo ====================================================
echo  🚀 AUTOMATIZADOR DE SUBIDAS A GITHUB
echo ====================================================
echo.

:: 1. Añadir todos los cambios
echo 📦 Añadiendo cambios al área de preparación...
git add .
if %errorlevel% neq 0 (
    echo ❌ Error al ejecutar 'git add'. Proceso cancelado.
    goto final
)
echo Verificando estado actual de los archivos:
git status -s
echo.

:: 2. Solicitar el mensaje del commit al usuario
echo ✍️  Escribe la descripción de los cambios realizados y pulsa ENTER:
set /p mensaje=""

:: Validar que el usuario no haya dejado el mensaje en blanco
if "%mensaje%"=="" (
    echo ⚠️  El mensaje no puede estar vacío. Se asignará un mensaje por defecto.
    set mensaje=Actualización automática del bot
)
echo.

:: 3. Realizar el commit
echo 💾 Guardando punto de control (Commit)...
git commit -m "%mensaje%"
if %errorlevel% neq 0 (
    echo ❌ Error al ejecutar 'git commit'. ¿Hay cambios nuevos realmente?
    goto final
)
echo.

:: 4. Subir al repositorio remoto
echo 📤 Subiendo archivos a GitHub (origin main)...
git push origin main
if %errorlevel% neq 0 (
    echo ❌ Error al ejecutar 'git push'. Verifica tu conexión o el estado remoto.
    goto final
)

echo.
echo ====================================================
echo  ✅ ¡PROCESO COMPLETADO CON ÉXITO!
echo ====================================================

:final
echo.
pause
