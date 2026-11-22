# SIM7600 Home Assistant Integration

This file contains the architectural standards, conventions, and workflows for this repository.

## Architecture
- **Framework:** Home Assistant integration (HACS-compatible).
- **Core Logic:** Located in `custom_components/sim7600/`.
- **Async:** All I/O operations must be performed asynchronously to prevent blocking the Home Assistant event loop.

## Standards
- **Language:** Python 3.12+.
- **Typing:** Strict type hinting is required for all modules.
- **Linting/Formatting:** `ruff` is the primary tool for linting and formatting.
- **Testing:** `pytest` is used for all testing. Ensure new features or bug fixes have corresponding tests in the `tests/` directory.

## Development Workflow
1. **Research:** Explore codebase using `grep_search` and `glob`.
2. **Strategy:** Plan changes before execution.
3. **Execution:** 
   - Apply changes surgically (incorporate logging: TRACE for raw serial, DEBUG for AT commands).
   - Run local CI parity checks: `ruff`, `mypy`, `safety`, `pytest`.
4. **Validation:** Analyze `git diff` to ensure all changes are intentional and logical.
5. **Synchronization:** 
   - Ensure local CI parity passes.
   - Perform Git synchronization (push/pull) on `main` only after successful local validation.
6. **CI Monitoring:** Monitor the GitHub Actions pipeline for the pushed changes.
7. **Autonomous Fix:** If CI fails, analyze logs in Plan Mode and apply fixes until CI is green.
