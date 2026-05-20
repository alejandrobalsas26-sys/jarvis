#!/usr/bin/env bash
# setup.sh — Instala dependencias del proyecto JARVIS
# Funciona en Linux (Kali/Ubuntu) y macOS
# En Windows: ejecuta los comandos pip manualmente en PowerShell

set -e

echo "=== JARVIS Setup ==="

# Verifica Python 3.10+
python3 --version

# Crea entorno virtual si no existe
if [ ! -d ".venv" ]; then
    echo "[1/4] Creando entorno virtual..."
    python3 -m venv .venv
fi

# Activa entorno virtual
source .venv/bin/activate 2>/dev/null || source .venv/Scripts/activate

echo "[2/4] Actualizando pip..."
pip install --upgrade pip -q

echo "[3/4] Instalando dependencias..."
pip install -r requirements.txt

echo "[4/4] Configuración de entorno..."
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo ""
    echo "⚠  Archivo .env creado. EDÍTALO antes de correr:"
    echo "   → Agrega tu ANTHROPIC_API_KEY"
    echo "   → Ajusta ASSISTANT_NAME, USER_NAME, CITY"
else
    echo "   .env ya existe, no se sobreescribió."
fi

echo ""
echo "=== Listo ==="
echo "Para correr:"
echo "  source .venv/bin/activate"
echo "  python main.py            # modo texto"
echo "  python main.py --voice    # modo voz"
