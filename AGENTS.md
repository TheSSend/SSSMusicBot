# Musicbot UI and Figma Rules

## Scope
- This repository is a Python `aiohttp` bot/admin panel project.
- The admin UI is rendered from `web_admin.py` using inline HTML/CSS helpers.
- Keep changes small, safe, and reversible.

## UI Architecture
- Preserve the existing server-rendered dashboard pattern.
- Do not introduce a frontend framework unless explicitly requested.
- Keep HTML generation in `web_admin.py` and data access in the existing config helpers.
- Prefer additive changes over rewrites.

## Design Language
- Use a modern dark admin/dashboard style.
- Prefer card layouts, sidebar navigation, badges/chips, and readable hierarchy.
- Keep spacing consistent and avoid cramped forms.
- When showing Discord IDs, also show resolved names when the bot cache can resolve them.
- Show effective values from `web_config.json` plus `.env` fallback, not just raw stored IDs.

## Data Display Rules
- For role/channel/user references, render `Name (ID)` when possible.
- Fallback to raw IDs only if the name cannot be resolved from Discord cache.
- For lists of IDs, show a compact badge list in the UI.
- Always include a raw JSON/config preview for debugging.
- Do not hide existing values in forms; prefill them from the active config.

## Figma Workflow
- If a Figma design or screen is requested, start from the current dashboard structure.
- Keep the same visual language across Dashboard, Music, Environment, Module settings, and Logs.
- Use a sidebar + topbar + card layout in the design.
- Resolve names for Discord entities in the design notes so the implementation mirrors real data.
- Prefer a single coherent admin dashboard over separate unrelated screens.

## Safety
- Do not break save endpoints or state restore flows while improving presentation.
- Avoid large refactors in the same change as UI polish.
- Verify any UI change with a syntax check before finishing.
