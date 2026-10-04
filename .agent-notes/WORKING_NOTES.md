# HyperQwen UI — Working Notes
Started: 2026-10-03 ~10:53 +0200 · Windows · cmd · cwd=G:\Apps\HyperQwen

## DONE
- Explored repo. HyperQwen = Qwen3.8-27B serving stack on vLLM 0.30 (Docker).
- Config is a .env of env vars. Launch via docker compose --profile single|batch.
- Read: README, resolve_config.sh, resolve_api_key.sh, Makefile, docker-compose.yml,
        .env.example, .env (masked), single-user/README (knobs table), batch/README (knobs table)
- Full settings inventory written -> SETTINGS_SCHEMA.md (same dir)
- Current .env: SPEC=dflash2 PREFIX_CACHE=1 CTX=long VLLM_WSL2_ENABLE_PIN_MEMORY=1 + key + HF_TOKEN

## PLAN (UI)
- Zero-dependency: Python stdlib HTTP server (ui/serve.py) + one index.html (vanilla JS/CSS).
- Server (bind 127.0.0.1, port 8790):
    GET  /                  -> index.html
    GET  /api/settings      -> {schema(groups), current values, raw, backups, hasKey}
    POST /api/settings      -> validate enums, write .env w/ timestamped .env.bak-<ts> (preserve comments)
    POST /api/keygen        -> openssl-style hex key into VLLM_API_KEY
    GET  /api/status        -> probe 127.0.0.1:PORT/health, docker present?, mode
    POST /api/action        -> {up-single|up-batch|down|ps|logs} via docker compose (repo root)
    GET  /api/backups ; POST /api/restore
- .env WRITE MODEL (safe/idempotent/preserves comments + unknown keys):
    per managed key K, new value V:
      V set: edit active K=line | else un-comment template '# K=' line | else append K=V
      V empty: comment out active line as '# K=' (disable, keep doc); else noop
    secrets use __KEEP__ sentinel to preserve current.
- Presets A-E = client sets values then optional launch.
- Entry: "Start HyperQwen Settings.bat" (python ui\serve.py + open browser) + ui/README.md
- UI: grouped form, right control per type, plain-English desc, preset buttons,
      status panel (health, warnings), Save (backup), keygen, launch buttons, log viewer.
      WARNINGs: CTX=long+SPEC=dflash2 mismatch; no API key while port exposed; WSL2 pin mem.

## TODO
- [x] explore
- [x] find config + format
- [x] enumerate settings
- [ ] check runtimes (python/docker)  <-- now
- [ ] build serve.py + schema.json
- [ ] build index.html
- [ ] bat launcher + README
- [ ] TEST: start server, load, change value, confirm .env updated + backup made
- [ ] write user "how to use"

## RISKS
- No local Python? -> fallback needed (PowerShell server, or instruct install). Checking now.
- User likely uses Docker Desktop -> actions call docker compose at repo root.
  
"## 2026-10-03 follow-up fixes"  
"1. Bool fields: replaced pill-toggle (.switch/.tr, hidden input) with plain accent-color checkbox (app.js controlFor + index.html CSS)."  
"2. Presets: A and B had empty sets {} so did nothing; all 5 presets now carry complete 10-key sets (SPEC/CTX/DFLASH_TOKENS/DRAFT_TOKENS/KV/INT8_ACT/INT8_LAYERS/MAX_SEQS/MAX_LEN/GPU_UTIL, empty=clear). Values from single-user/batch READMEs: B=dflash2/fast/65536/8/0.93, C=+DFLASH_TOKENS=15, D=mtp/long/DRAFT_TOKENS=3/150000, E=dflash2/huge/200000, A=batch fp8/int8/mlp/64/150000/0.95. applyPreset now re-renders presets so the 'current' tag moves."  
"3. Tested: node --check OK, schema served A-E:10, no-op save left .env byte-identical (fc /b), server killed, temp files cleaned."  
  
"## 2026-10-03 12:15 - NetworkError incident"  
"Cause: my test used 'taskkill /f /im python.exe' which killed the USER's running server; browser tab was a restored stale page, so save fetch got connection refused (Firefox 'NetworkError')."  
"Fixes: (1) serve.py do_POST now wraps dispatch in try/except -> JSON 500 'server error: ...' instead of dropped connection. (2) app.js api() catches fetch failures -> friendly 'Can't reach the settings server - is the black command window still open?'"  
"Verified: full 28-key browser-style save (preset B + __KEEP__ secrets) OK, secrets preserved, .env restored byte-identical, test backups deleted, both stray test servers killed BY PID (never taskkill /im python.exe again!), port 8790 free."  
  
"## 2026-10-03 12:25 - save button bug"  
"renderGroups attached input listeners via getElementById BEFORE box.appendChild(card) -> detached card -> null -> NO listeners anywhere. Presets seemed to work only because applyPreset calls setField+refreshChanged directly. Fix: appendChild before attaching listeners. node --check OK."  
