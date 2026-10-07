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



## 2026-10-07 ~13:30 - UI inference (sampling) defaults: TEMPERATURE / TOP_P / TOP_K / MIN_P / PRESENCE_PENALTY / REPETITION_PENALTY



"Feature: the 6 keys became a first inference group in ui/schema.json (app.js is schema-driven, no app.js change); blank = the model's own generation_config default; per-request values still win; both start_qwen.sh turn non-blank values into ONE --override-generation-config via build_sampling_args() in resolve_config.sh (range-checked: non-numeric or out-of-range dropped with a stderr note), expanded before EXTRA_ARGS so a user flag still wins. serve.py validate() now range-checks every int/float field that declares min/max (fail-closed, 400 on a bad value; finite-number check). .env.example got 6 commented keys; SETTINGS_SCHEMA.md got an INFER section; the ui/README bullet is in."

"Recovery incident: the earlier python applier (apply_edits.py v1) opened resolve_config.sh for write and then crashed on an anchor assert, truncating the file to 0 bytes (the earlier FileTools strReplace batch really was a no-op - it left no undo history). Restored from git: git restore -- resolve_config.sh (104-line HEAD version; the file had no uncommitted changes). Lesson: never trust a batch text-replace that reports ok without a post-write sentinel probe, and a one-shot applier must verify anchors BEFORE opening files for write."

"Verification: --override-generation-config confirmed in the live image ghcr.io/syv-ai/hyperqwen:latest (vllm serve --help) and in the v0.30.0 arg_utils.py source. The user's single-user server stayed up the whole time (not touched - restart needed to pick up new keys). UI round-trip test and bash -n results: closing line follows. 2026-10-07 13:35 - test_ui.py: ALL PASS (inference group first, six keys, valid POST 200, TEMPERATURE=0.9 written, out-of-range 400, non-numeric 400, blank->commented line, .env restored byte-identical). bash -n x3 via Git Bash: BASH_N_OK. LATE IN-CONTAINER PROBE: image vllm reports 0.30.0 but its EngineArgs/CLI lack --override-generation-config (custom build; serve --help 34 lines, no usage/override) - so build_sampling_args() now feature-checks vllm serve --help and exits 97 with a clear stderr message when a sampling key is set on an unsupported build. Keys blank (the default) = flag not emitted = running server unaffected. Re-verify the flag when the image's vllm changes."
