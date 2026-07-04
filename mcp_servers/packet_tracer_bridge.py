from mcp.server.fastmcp import FastMCP
from pathlib import Path
import subprocess
import os

# Inicializamos el servidor MCP
mcp = FastMCP("Cisco-PT-Bridge")

_OUTPUT_DIR = Path(os.path.expanduser("~")) / "Downloads"


@mcp.tool()
def generar_laboratorio_red(xml_content: str, nombre_archivo: str = "laboratorio_ia.pkt"):
    """
    Crea un archivo .pkt compatible con Cisco Packet Tracer inyectando XML.
    """
    # SECURITY: nombre_archivo is caller-controlled (ultimately model-chosen)
    # input. Reject anything that isn't a bare filename BEFORE it reaches
    # os.path.join — a value like "../../Windows/System32/x" or an absolute
    # "C:\\..." path would otherwise let an MCP tool call write anywhere the
    # process can reach. Path(name).name strips any traversal/drive/UNC
    # component; comparing it back to the original catches an attempt.
    if not nombre_archivo or Path(nombre_archivo).name != nombre_archivo or nombre_archivo in (".", ".."):
        raise ValueError(f"nombre_archivo inválido (path traversal no permitido): {nombre_archivo!r}")

    path = _OUTPUT_DIR / nombre_archivo
    resolved = path.resolve()
    if not resolved.is_relative_to(_OUTPUT_DIR.resolve()):
        raise ValueError(f"nombre_archivo inválido (fuera del directorio permitido): {nombre_archivo!r}")

    # Aquí es donde ocurre la 'magia': empaquetamos el XML en el formato de Cisco
    # Para simplificar el MVP, guardamos el XML directamente para análisis
    with open(resolved, "w") as f:
        f.write(xml_content)

    return f"Laboratorio generado en: {resolved}. Ábrelo con Packet Tracer."

@mcp.tool()
def abrir_packet_tracer():
    """Lanza la instancia de Cisco Packet Tracer en el host."""
    try:
        # Ruta estándar en Windows
        cmd = r"C:\Program Files\Cisco\Cisco Packet Tracer 8.2\bin\PacketTracer.exe"
        subprocess.Popen([cmd], shell=False)
        return "Packet Tracer iniciado exitosamente."
    except Exception as e:
        return f"Error al abrir: {str(e)}"

if __name__ == "__main__":
    mcp.run()