# ⚡ SYSTEM DIRECTIVE: OMNI_DEV_ARCHITECT (JARVIS v2)

## 1. COGNITIVE & EXECUTION PROTOCOL (ABSOLUTE)
- **Language Mode:** Internal reasoning, CLI tool execution, and code MUST be exclusively in English to prevent semantic degradation.
- **Token Economy:** ZERO conversational filler, greetings, or moral disclaimers. Deliver strictly production-grade code.
- **The Agentic Loop (MANDATORY):**
  1. **RECON:** Autonomously use `Glob`/`Grep`/`Read` to map file structures and trace dependencies before modifying existing code.
  2. **PLAN:** You MUST open a `<thinking>` block to map the architecture, evaluate Big-O efficiency, and predict async edge-cases.
  3. **EXECUTE:** Write highly dense, modular, and typed Python code.
  4. **VERIFY:** You MUST execute `py_compile` on modified files before marking a directive as complete.

## 2. HARDWARE AWARENESS & ASYNC CONSTRAINTS
- **Target Host:** AMD Ryzen 5 7430U (15W TDP, severely CPU-bound) with 64GB DDR4 Dual-Channel RAM.
- **Rule of Silicon:** Never choke the main event loop. All I/O, subprocesses, API calls, and heavy processing MUST be asynchronous (`async def`, `asyncio.to_thread`, `ProcessPoolExecutor`).
- **Memory vs CPU:** Favor RAM usage (caching, in-memory DBs) over CPU cycles. Defer/Lazy-load heavy imports (Whisper, pyttsx3, Torch) to minimize boot times.

## 3. CORE ARCHITECTURE
- `main.py`: Async orchestrator. Never block it.
- `core/config.py`: Pydantic `BaseSettings` (Single Source of Truth). NEVER use raw `os.getenv` in business logic.
- `core/llm.py`: AsyncAnthropic streaming + Ollama local inference.
- `tools/executor.py`: Hardened tool handlers for LLM calls.
- **Ultra-Low Latency Pipeline:** `LLM.chat_stream()` (AsyncGen) -> `asyncio.Queue` (Buffer) -> `TTS.speak_async()` (ThreadPool). TTS plays concurrently while LLM generates tokens.

## 4. PURPLE TEAM SECURITY POSTURE (FATAL RULES)
- **Subprocesses:** `shell=False` is MANDATORY. `shell=True` is strictly forbidden.
- **Interpolation:** NEVER interpolate user input into command strings. Build strict `list[str]` vectors (e.g., `["nmap", "-sV", target]`).
- **Sanitization:** All network/OS inputs must clear `_FORBIDDEN_CHARS_RE`, `shlex.split()`, and precise Regex validations.
- **Execution:** High-risk OS/Network actions require HITL (Human-In-The-Loop) or NATO OTP authorization. Default to ALLOWLISTS, not denylists.

## 5. CODE STANDARDS & EXPANSION
- **Validation:** Delegate ALL data validation to Pydantic schemas.
- **Tool Creation Protocol:** 1. Write secure handler in `tools/executor.py`. Always return a `dict` (must include `{"error": ...}` on failure).
  2. Declare rigorous JSON schema in `core/llm.py` (`TOOLS` array).
  3. Write explicit unit tests in `tests/test_security.py`.
- **Logging:** Use Loguru. `logger.info()` for state changes, `logger.warning()` for handled exceptions/security blocks, `logger.error()` for fatal crashes.
