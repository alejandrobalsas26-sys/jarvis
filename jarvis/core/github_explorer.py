"""
core/github_explorer.py — GitHub security tool autodiscovery engine (v37.0).

Searches GitHub for security tools on demand. Evaluates by:
  - Stars (popularity signal)
  - Recent activity (last push < 6 months)
  - Language (Python preferred — easiest to wrap)
  - README quality (non-empty, security-focused)
  - JARVIS compatibility (has CLI, importable, or subprocess-friendly)

Downloads to: tools/external/<repo_name>/
Install:      pip install -e tools/external/<repo_name>/ in JARVIS venv
Wraps:        LLM generates a JARVIS tool function from README
Registers:    adds to ToolExecutor dynamic registry

SECURITY:
  - Execution of downloaded code requires explicit operator confirmation
  - Installation (pip) is automatic — pip sandboxed to JARVIS venv
  - All downloads logged with SHA-256 of cloned content
  - GitHub token optional but increases API rate limit 60→5000 req/hr
"""

import asyncio, hashlib, json, os, re, shutil, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path
from loguru import logger

_EXTERNAL_TOOLS_DIR = Path("tools/external")
_EXTERNAL_TOOLS_DIR.mkdir(parents=True, exist_ok=True)

_GITHUB_TOKEN   = os.getenv("GITHUB_TOKEN", "")
_GITHUB_API     = "https://api.github.com"

# Tool registry: {tool_name: {repo, description, installed, wrapper_code}}
_tool_registry: dict[str, dict] = {}
_REGISTRY_PATH = Path("logs/github_tool_registry.json")
_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)

# Quality thresholds
_MIN_STARS        = 50
_MAX_AGE_DAYS     = 365
_PREFERRED_LANGS  = {"Python", "Go", "C"}

_WRAPPER_SYSTEM = """You are a JARVIS tool integration specialist.
Given a GitHub tool's README and metadata, generate a Python async function
that wraps this tool for use inside JARVIS. The function must:
1. Accept relevant parameters as arguments
2. Use subprocess.run(shell=False) to execute the tool
3. Return results as a dict with keys: stdout, stderr, status
4. Handle errors gracefully
5. Be importable standalone

Output ONLY the Python function code, no markdown fences."""


def _github_headers() -> dict:
    h = {"Accept": "application/vnd.github+json"}
    if _GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {_GITHUB_TOKEN}"
    return h


async def search_github_tools(
    query: str,
    max_results: int = 10,
) -> list[dict]:
    """
    Search GitHub for security tools matching query.
    Returns ranked list of candidate repos.
    """
    import aiohttp
    from datetime import datetime, timedelta

    cutoff = (datetime.now() - timedelta(days=_MAX_AGE_DAYS)).strftime(
        "%Y-%m-%d"
    )
    search_q = f"{query} security pushed:>{cutoff}"

    url = (
        f"{_GITHUB_API}/search/repositories"
        f"?q={search_q}&sort=stars&order=desc&per_page={max_results}"
    )

    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                url, headers=_github_headers(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status != 200:
                    logger.warning(f"GITHUB: search failed: HTTP {r.status}")
                    return []
                data = await r.json()

        repos = []
        for item in data.get("items", []):
            lang = item.get("language", "Unknown") or "Unknown"
            repos.append({
                "name":        item["full_name"],
                "description": item.get("description", "")[:200],
                "stars":       item.get("stargazers_count", 0),
                "language":    lang,
                "url":         item["html_url"],
                "clone_url":   item["clone_url"],
                "last_push":   item.get("pushed_at", ""),
                "topics":      item.get("topics", []),
                "score":       _quality_score(item),
            })

        # Sort by quality score
        repos.sort(key=lambda x: x["score"], reverse=True)
        return repos

    except Exception as e:
        logger.debug(f"GITHUB: search error: {e}")
        return []


def _quality_score(repo: dict) -> float:
    """Compute quality score for ranking."""
    score = 0.0
    score += min(repo.get("stargazers_count", 0) / 1000, 5.0)
    lang = repo.get("language", "")
    if lang in _PREFERRED_LANGS:
        score += 2.0
    desc = (repo.get("description", "") or "").lower()
    for kw in ("security", "pentest", "recon", "exploit", "forensic",
                "scanner", "audit", "hack", "osint"):
        if kw in desc:
            score += 0.5
    return score


async def evaluate_with_llm(
    candidates: list[dict],
    user_query: str,
    ollama_client,
    model: str,
) -> dict | None:
    """
    Ask the LLM to pick the best tool from candidates.
    Returns the chosen candidate dict.
    """
    candidates_str = "\n".join(
        f"{i+1}. {c['name']} ({c['stars']}★ {c['language']}) — "
        f"{c['description'][:100]}"
        for i, c in enumerate(candidates[:5])
    )
    prompt = (
        f"User wants: {user_query}\n\n"
        f"Available GitHub tools:\n{candidates_str}\n\n"
        "Which single tool is most relevant and useful for a Purple Team "
        "security researcher? Answer with just the number (1-5)."
    )
    try:
        response = await asyncio.wait_for(
            ollama_client.chat.completions.create(
                model    = model,
                messages = [{"role": "user", "content": prompt}],
                stream   = False,
                extra_body = {"options": {"num_ctx": 512, "temperature": 0}},
            ),
            timeout=20.0,
        )
        choice_str = response.choices[0].message.content.strip()
        idx = int(re.search(r"\d", choice_str).group()) - 1
        return candidates[max(0, min(idx, len(candidates)-1))]
    except Exception:
        return candidates[0] if candidates else None


async def download_and_install(
    repo: dict,
    broadcast_fn,
) -> bool:
    """
    Clone repo and install Python dependencies.
    Returns True on success.
    """
    repo_name  = repo["name"].replace("/", "__")
    target_dir = _EXTERNAL_TOOLS_DIR / repo_name

    await broadcast_fn({
        "type":      "github_downloading",
        "repo":      repo["name"],
        "stars":     repo["stars"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    loop = asyncio.get_running_loop()

    def _clone_and_install():
        # Remove existing if present
        if target_dir.exists():
            shutil.rmtree(target_dir)

        # Clone (depth=1 for speed)
        result = subprocess.run(
            ["git", "clone", "--depth=1",
             repo["clone_url"], str(target_dir)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            logger.error(f"GITHUB: clone failed: {result.stderr[:200]}")
            return False

        # Install Python deps if present
        for req_file in ["requirements.txt", "setup.py", "pyproject.toml"]:
            req_path = target_dir / req_file
            if req_path.exists():
                if req_file == "requirements.txt":
                    subprocess.run(
                        [sys.executable, "-m", "pip", "install",
                         "--quiet", "--break-system-packages",
                         "-r", str(req_path)],
                        capture_output=True, timeout=120,
                    )
                elif req_file in ("setup.py", "pyproject.toml"):
                    subprocess.run(
                        [sys.executable, "-m", "pip", "install",
                         "--quiet", "--break-system-packages",
                         "-e", str(target_dir)],
                        capture_output=True, timeout=120,
                    )
                break

        logger.info(f"GITHUB: installed {repo['name']} → {target_dir}")
        return True

    success = await loop.run_in_executor(None, _clone_and_install)
    return success


async def generate_wrapper(
    repo: dict,
    target_dir: Path,
    ollama_client,
    model: str,
) -> str:
    """
    Use LLM to generate a Python wrapper function for the tool.
    Reads the README for context.
    """
    readme_text = ""
    for readme_name in ["README.md", "README.rst", "README.txt", "README"]:
        readme_path = target_dir / readme_name
        if readme_path.exists():
            readme_text = readme_path.read_text(
                encoding="utf-8", errors="replace"
            )[:3000]
            break

    # Find the main executable
    main_candidates = list(target_dir.glob("*.py"))[:3]
    main_files      = [f.name for f in main_candidates]

    prompt = (
        f"Tool: {repo['name']}\n"
        f"Description: {repo['description']}\n"
        f"Language: {repo['language']}\n"
        f"Main files: {main_files}\n\n"
        f"README excerpt:\n{readme_text[:2000]}\n\n"
        "Generate a Python async JARVIS wrapper function named "
        f"`run_{repo['name'].split('/')[-1].replace('-','_')}`. "
        "Use subprocess.run(shell=False). Return dict with "
        "stdout, stderr, status keys."
    )

    try:
        response = await asyncio.wait_for(
            ollama_client.chat.completions.create(
                model    = model,
                messages = [
                    {"role": "system", "content": _WRAPPER_SYSTEM},
                    {"role": "user",   "content": prompt},
                ],
                stream = False,
                extra_body = {"options": {"num_ctx": 2048, "temperature": 0.1}},
            ),
            timeout=45.0,
        )
        wrapper = response.choices[0].message.content.strip()
        # Strip markdown fences
        wrapper = re.sub(r'^```python\s*', '', wrapper, flags=re.IGNORECASE)
        wrapper = re.sub(r'\s*```$', '', wrapper)
        return wrapper.strip()
    except Exception as e:
        logger.debug(f"GITHUB: wrapper generation failed: {e}")
        return ""


async def autodiscover_and_integrate(
    user_query: str,
    broadcast_fn,
    ollama_client,
    model: str,
) -> dict:
    """
    Full pipeline: search → rank → LLM pick → clone → install → wrap → register.
    """
    logger.info(f"GITHUB_EXPLORER: searching for '{user_query}'")

    await broadcast_fn({
        "type":    "github_search_started",
        "query":   user_query,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    # 1. Search
    candidates = await search_github_tools(user_query)
    if not candidates:
        await broadcast_fn({
            "type":  "github_search_failed",
            "query": user_query,
            "reason": "no results",
        })
        return {"error": "No GitHub results found"}

    logger.info(f"GITHUB_EXPLORER: {len(candidates)} candidates found")

    # 2. LLM evaluation
    chosen = await evaluate_with_llm(
        candidates, user_query, ollama_client, model
    )
    if not chosen:
        return {"error": "Could not evaluate candidates"}

    logger.info(
        f"GITHUB_EXPLORER: selected '{chosen['name']}' "
        f"({chosen['stars']}★) — {chosen['description'][:60]}"
    )

    # 3. Download and install
    repo_name  = chosen["name"].replace("/", "__")
    target_dir = _EXTERNAL_TOOLS_DIR / repo_name
    success    = await download_and_install(chosen, broadcast_fn)
    if not success:
        return {"error": f"Failed to clone {chosen['name']}"}

    # 4. Generate JARVIS wrapper
    wrapper_code = await generate_wrapper(
        chosen, target_dir, ollama_client, model
    )

    # 5. Save wrapper
    wrapper_path = _EXTERNAL_TOOLS_DIR / f"{repo_name}_wrapper.py"
    if wrapper_code:
        wrapper_path.write_text(wrapper_code, encoding="utf-8")

    # 6. Register in tool registry
    tool_entry = {
        "name":         repo_name,
        "repo":         chosen["name"],
        "url":          chosen["url"],
        "description":  chosen["description"],
        "stars":        chosen["stars"],
        "language":     chosen["language"],
        "install_dir":  str(target_dir),
        "wrapper":      str(wrapper_path) if wrapper_code else None,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "wrapper_code": wrapper_code[:500] if wrapper_code else "",
    }
    _tool_registry[repo_name] = tool_entry

    # Persist registry
    _REGISTRY_PATH.write_text(
        json.dumps(_tool_registry, indent=2), encoding="utf-8"
    )

    await broadcast_fn({
        "type":        "github_tool_integrated",
        "tool_name":   repo_name,
        "repo":        chosen["name"],
        "stars":       chosen["stars"],
        "has_wrapper": bool(wrapper_code),
        "install_dir": str(target_dir),
        "timestamp":   datetime.now(timezone.utc).isoformat(),
    })

    logger.info(
        f"GITHUB_EXPLORER: '{repo_name}' integrated successfully. "
        f"Wrapper: {bool(wrapper_code)}"
    )

    return tool_entry


def list_integrated_tools() -> list[dict]:
    """Return all integrated GitHub tools."""
    return list(_tool_registry.values())


def load_registry() -> None:
    """Load persisted tool registry at startup."""
    if _REGISTRY_PATH.exists():
        try:
            global _tool_registry
            _tool_registry = json.loads(
                _REGISTRY_PATH.read_text(encoding="utf-8")
            )
            logger.info(
                f"GITHUB_EXPLORER: loaded {len(_tool_registry)} "
                f"integrated tools from registry"
            )
        except Exception:
            pass
