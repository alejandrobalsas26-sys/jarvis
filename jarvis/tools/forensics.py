"""tools/forensics.py — EXIF-based image forensics with GPS → Cartesian mapping."""

import asyncio
import math
from pathlib import Path

import piexif


def _rational_to_float(rational: tuple) -> float:
    num, denom = rational
    return num / denom if denom else 0.0


def _dms_to_decimal(dms: tuple, ref: str) -> float:
    deg     = _rational_to_float(dms[0])
    minutes = _rational_to_float(dms[1])
    seconds = _rational_to_float(dms[2])
    decimal = deg + minutes / 60 + seconds / 3600
    if ref in ("S", "W"):
        decimal = -decimal
    return decimal


def _latlon_to_cartesian(
    lat_deg: float, lon_deg: float, radius: float = 200.0
) -> tuple[float, float, float]:
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    x = radius * math.cos(lat) * math.cos(lon)
    y = radius * math.sin(lat)
    z = radius * math.cos(lat) * math.sin(lon)
    return round(x, 4), round(y, 4), round(z, 4)


async def analyze_image_forensics(file_path: str) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _extract_exif, file_path)


def _extract_exif(file_path: str) -> dict:
    path = Path(file_path)
    if not path.exists():
        return {"error": f"File not found: {file_path}"}

    try:
        exif_data = piexif.load(str(path))
    except Exception as exc:
        return {"error": f"EXIF parse failed: {exc}"}

    gps    = exif_data.get("GPS", {})
    result: dict = {"file": path.name, "has_gps": False}

    if piexif.GPSIFD.GPSLatitude in gps and piexif.GPSIFD.GPSLongitude in gps:
        lat_ref = gps.get(piexif.GPSIFD.GPSLatitudeRef, b"N").decode()
        lon_ref = gps.get(piexif.GPSIFD.GPSLongitudeRef, b"E").decode()
        lat = _dms_to_decimal(gps[piexif.GPSIFD.GPSLatitude], lat_ref)
        lon = _dms_to_decimal(gps[piexif.GPSIFD.GPSLongitude], lon_ref)
        x, y, z = _latlon_to_cartesian(lat, lon)
        result.update({
            "has_gps": True,
            "latitude": round(lat, 6),
            "longitude": round(lon, 6),
            "cartesian": {"x": x, "y": y, "z": z},
        })

    return result
