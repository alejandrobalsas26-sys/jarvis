"""
tools/browser_intel.py — Playwright browser intelligence module (v38.0).

Full headless browser automation using Playwright Chromium.
Not webbrowser.open() — actual page content extraction + screenshot.

Capabilities:
  - Navigate to any URL and extract full text content
  - Screenshot pages for visual analysis
  - Auto-research: CVE pages, threat intel, GitHub READMEs
  - Monitor pages for content changes
  - Execute JavaScript for dynamic content extraction

Playwright installed in v37: playwright>=1.40.0 + chromium
"""

import asyncio, os
from datetime import datetime, timezone
from pathlib import Path
from loguru import logger

_SCREENSHOTS_DIR = Path("logs/visuals/browser")
_SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

# Allowlist of domains JARVIS can autonomously browse
# Operator can extend via JARVIS_BROWSE_ALLOWLIST env var
_ALLOWED_DOMAINS = {
    "nvd.nist.gov",
    "cve.mitre.org",
    "github.com",
    "cisa.gov",
    "attack.mitre.org",
    "shodan.io",
    "virustotal.com",
    "malpedia.caad.fkie.fraunhofer.de",
    "abuse.ch",
    "feodotracker.abuse.ch",
    "otx.alienvault.com",
}

# Load operator-defined extra domains
_extra = os.getenv("JARVIS_BROWSE_ALLOWLIST", "")
if _extra:
    _ALLOWED_DOMAINS.update(d.strip() for d in _extra.split(","))


def _domain_allowed(url: str) -> bool:
    """Check if URL domain is in the allowlist."""
    from urllib.parse import urlparse
    try:
        host = urlparse(url).netloc.lstrip("www.")
        return any(host == d or host.endswith("." + d)
                   for d in _ALLOWED_DOMAINS)
    except Exception:
        return False


async def browse_and_extract(
    url: str,
    broadcast_fn,
    take_screenshot: bool = True,
    extract_text: bool = True,
    js_query: str = "",
) -> dict:
    """
    Navigate to URL with Playwright.
    Returns: {url, text, screenshot_path, title, status}
    """
    if not _domain_allowed(url):
        logger.warning(f"BROWSER: domain not in allowlist: {url}")
        await broadcast_fn({
            "type":    "browser_blocked",
            "url":     url[:80],
            "reason":  "domain not in allowlist",
            "severity": "WARNING",
        })
        return {"error": "Domain not in allowlist",
                "url": url, "text": "", "screenshot_path": None}

    logger.info(f"BROWSER: navigating to {url[:80]}")
    await broadcast_fn({
        "type":      "browser_navigating",
        "url":       url[:80],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    result = {
        "url":             url,
        "text":            "",
        "screenshot_path": None,
        "title":           "",
        "status":          0,
    }

    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless = True,
                args     = ["--no-sandbox", "--disable-gpu"],
            )
            page = await browser.new_page()
            page.set_default_timeout(20000)

            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=20000,
            )
            result["status"] = response.status if response else 0
            result["title"]  = await page.title()

            # Extract text content
            if extract_text:
                text = await page.evaluate(
                    "() => document.body ? document.body.innerText : ''"
                )
                result["text"] = text[:8000]   # cap at 8K chars

            # Execute custom JS query
            if js_query:
                try:
                    js_result = await page.evaluate(js_query)
                    result["js_result"] = str(js_result)[:2000]
                except Exception:
                    pass

            # Screenshot
            if take_screenshot:
                ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"browser_{ts}.png"
                path     = _SCREENSHOTS_DIR / filename
                await page.screenshot(path=str(path), full_page=False)
                result["screenshot_path"] = str(path)

            await browser.close()

        logger.info(
            f"BROWSER: {url[:60]} → status={result['status']} "
            f"text={len(result['text'])}chars"
        )

        await broadcast_fn({
            "type":      "browser_complete",
            "url":       url[:80],
            "title":     result["title"][:80],
            "text_len":  len(result["text"]),
            "status":    result["status"],
            "screenshot": result["screenshot_path"] is not None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    except Exception as e:
        logger.debug(f"BROWSER: navigation error: {e}")
        result["error"] = str(e)
        await broadcast_fn({
            "type":    "browser_error",
            "url":     url[:80],
            "error":   str(e)[:100],
            "severity": "WARNING",
        })

    return result


async def research_cve(
    cve_id: str,
    broadcast_fn,
    ollama_client,
    vision_model: str = "moondream:latest",
) -> dict:
    """
    Auto-research a CVE: navigate NVD, extract details, screenshot.
    Returns full research dict.
    """
    url = f"https://nvd.nist.gov/vuln/detail/{cve_id}"
    logger.info(f"BROWSER: researching {cve_id}")

    page_data = await browse_and_extract(
        url, broadcast_fn, take_screenshot=True
    )

    result = {
        "cve_id":      cve_id,
        "nvd_url":     url,
        "page_text":   page_data.get("text", "")[:3000],
        "screenshot":  page_data.get("screenshot_path"),
        "title":       page_data.get("title", ""),
    }

    await broadcast_fn({
        "type":      "cve_researched",
        "cve_id":    cve_id,
        "url":       url,
        "has_screenshot": result["screenshot"] is not None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    return result


async def research_threat_actor(
    actor_name: str,
    broadcast_fn,
) -> dict:
    """Navigate to MITRE ATT&CK for threat actor profile."""
    slug = actor_name.lower().replace(" ", "-")
    url  = f"https://attack.mitre.org/groups/{slug}/"

    page_data = await browse_and_extract(
        url, broadcast_fn, take_screenshot=True
    )
    return {
        "actor":      actor_name,
        "url":        url,
        "profile":    page_data.get("text", "")[:3000],
        "screenshot": page_data.get("screenshot_path"),
    }


async def open_url_tactical(
    url: str,
    broadcast_fn,
    open_in_browser: bool = True,
) -> bool:
    """
    Open a URL — headless research + optionally visible browser.
    This is the proper version of Gemini's webbrowser.open().
    """
    import webbrowser

    # First do headless research (always)
    asyncio.create_task(
        browse_and_extract(url, broadcast_fn, take_screenshot=True)
    )

    # Open visible browser for operator (optional)
    if open_in_browser:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, webbrowser.open, url)
        logger.info(f"BROWSER: opened in default browser: {url[:60]}")

    return True
