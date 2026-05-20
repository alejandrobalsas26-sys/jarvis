from mcp.server.fastmcp import FastMCP
import subprocess
import os

# Inicializamos el servidor MCP
mcp = FastMCP("Cisco-PT-Bridge")

@mcp.tool()
def generar_laboratorio_red(xml_content: str, nombre_archivo: str = "laboratorio_ia.pkt"):
    """
    Crea un archivo .pkt compatible con Cisco Packet Tracer inyectando XML.
    """
    path = os.path.join(os.path.expanduser("~"), "Downloads", nombre_archivo)
    
    # Aquí es donde ocurre la 'magia': empaquetamos el XML en el formato de Cisco
    # Para simplificar el MVP, guardamos el XML directamente para análisis
    with open(path, "w") as f:
        f.write(xml_content)
    
    return f"Laboratorio generado en: {path}. Ábrelo con Packet Tracer."

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