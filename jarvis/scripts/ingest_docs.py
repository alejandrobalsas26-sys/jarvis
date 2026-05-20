"""
scripts/ingest_docs.py — Pobla ChromaDB con archivos locales o URLs.

Uso (desde jarvis_v2/jarvis/):
    python scripts/ingest_docs.py
    python scripts/ingest_docs.py --docs-path ../docs --chunk-size 400
    python scripts/ingest_docs.py --url https://example.com/article
    python scripts/ingest_docs.py --url https://example.com/a --url https://example.com/b
"""

import sys
import hashlib
import argparse
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# Permite importar core/ sin instalar el paquete
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.memory import VectorMemory  # noqa: E402


def _chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """Divide en chunks de `chunk_size` palabras con solapamiento de `overlap`."""
    words = text.split()
    chunks, i = [], 0
    while i < len(words):
        chunks.append(" ".join(words[i : i + chunk_size]))
        i += chunk_size - overlap
    return [c for c in chunks if c.strip()]


def _doc_id(source: str, chunk_idx: int) -> str:
    return hashlib.md5(f"{source}:{chunk_idx}".encode()).hexdigest()


def _fetch_url(url: str) -> str:
    """Descarga una URL y retorna su texto limpio (sin HTML)."""
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "aside"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingestor de documentos para ChromaDB")
    parser.add_argument(
        "--docs-path",
        default=str(Path(__file__).parent.parent.parent / "docs"),
        help="Carpeta de documentos (default: jarvis_v2/docs/)",
    )
    parser.add_argument("--chunk-size", type=int, default=500, help="Palabras por chunk")
    parser.add_argument(
        "--url",
        action="append",
        dest="urls",
        metavar="URL",
        default=[],
        help="URL a descargar y vectorizar (puede repetirse para múltiples URLs)",
    )
    args = parser.parse_args()

    memory = VectorMemory()
    total_chunks = 0

    # ── Modo URL ──────────────────────────────────────────────────────────────
    if args.urls:
        for url in args.urls:
            try:
                text = _fetch_url(url)
                if not text.strip():
                    print(f"[!] Sin texto extraíble: {url}")
                    continue
                chunks = _chunk_text(text, chunk_size=args.chunk_size)
                for i, chunk in enumerate(chunks):
                    memory.add(chunk, _doc_id(url, i), {"source": url, "chunk": i})
                print(f"[OK] {url} → {len(chunks)} chunks")
                total_chunks += len(chunks)
            except Exception as e:
                print(f"[!] Error procesando {url}: {e}")
        print(f"\nTotal: {total_chunks} chunks indexados de {len(args.urls)} URL(s).")
        return

    # ── Modo archivos locales ─────────────────────────────────────────────────
    docs_path = Path(args.docs_path)
    if not docs_path.exists():
        print(f"[!] Carpeta no encontrada: {docs_path}")
        print("    Crea jarvis_v2/docs/ y agrega archivos .txt o .md")
        sys.exit(1)

    files = list(docs_path.glob("**/*.txt")) + list(docs_path.glob("**/*.md"))
    if not files:
        print(f"[!] No se encontraron archivos .txt o .md en {docs_path}")
        sys.exit(0)

    for file_path in sorted(files):
        try:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
            chunks = _chunk_text(text, chunk_size=args.chunk_size)
            source = str(file_path.relative_to(docs_path))

            for i, chunk in enumerate(chunks):
                memory.add(chunk, _doc_id(source, i), {"source": source, "chunk": i})

            print(f"[OK] {source} → {len(chunks)} chunks")
            total_chunks += len(chunks)
        except Exception as e:
            print(f"[!] Error procesando {file_path.name}: {e}")

    print(f"\nTotal: {total_chunks} chunks indexados de {len(files)} archivo(s).")


if __name__ == "__main__":
    main()
