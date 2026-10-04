# HyperQwen Settings UI

A small, local-only web page for viewing and editing HyperQwen's settings —
no terminal needed. It edits the one file HyperQwen uses for configuration:
the .env file in the HyperQwen folder.

## Run it

Double-click **Start HyperQwen Settings.bat** (in the HyperQwen folder).
A browser window opens at http://127.0.0.1:8790
Stop it by closing the black window (or Ctrl-C in it).

Requirements: Python 3 (anything 3.8+). No packages to install — it uses
only the Python standard library.

## What you can do

- **Presets** — A to E. Click one to fill in the matching settings, then
  **Save**. Preset A = batch mode, B–E = single-user variants.
- **Settings groups** — every real .env knob, with plain-English help text.
  Changing a field marks it; **Save changes** writes them to .env.
- **Server card** — Start single / Start batch / Stop (docker compose),
  status (ps), logs, and a health dot in the top bar.
- **API key** — "Generate new API key" writes a fresh random key to .env.
- **Backups** — every save first makes a timestamped copy
  (.env.bak-YYYYmmdd-HHMMSS next to .env). Click **restore** on any to go
  back (restoring makes a backup of the current file too).
- **Raw .env** — live view of the actual file, secrets hidden.

## Safety notes

- The server binds to **127.0.0.1 only** — nothing is exposed to your network.
- Secret values (API key, HF token) are never shown in full, only as
  first4…(N chars). Leaving the secret field blank on save keeps the
  current value.
- Saves preserve your comments and any keys this UI doesn't know about.
- Setting a field to blank (or toggling a switch off) comments the line
  out in .env — that's how HyperQwen disables a setting.
- **Save, then Start**: changing settings only takes effect after you
  restart the server with one of the Start buttons.

## Files

| file | what it is |
|---|---|
| serve.py | the local web server (Python stdlib only) |
| index.html | the page |
| app.js | the page logic |
| schema.json | the list of settings, their types, help text, and presets |

To add or fix a setting's help text, edit schema.json and reload the page.
