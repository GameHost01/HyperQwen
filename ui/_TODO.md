# VRAM Calculator Phase 2 — TODO

## COMPLETED
- [x] serve.py: compute_vram() with 13 params, variable k, KV_MEM override, vision offload
- [x] serve.py: /api/vram passes all params with .env fallbacks
- [x] index.html: 14 VRAM controls + Sync button
- [x] app.js: syncVramFromSettings(), vramParams(), auto-sync on tab switch
- [x] Syntax check both files (PY_OK, JS_OK)
- [x] API tests: 10 param combos all correct
- [x] HTML/JS served correctly with all new element IDs
- [x] Test server killed

## USER ACTION NEEDED
- [ ] Restart UI server: `python ui\serve.py --no-browser` (port 8790)
- [ ] Open browser → VRAM Calculator tab → click "Sync from Settings"
- [ ] Verify it shows current .env values (mtp/long/150000/0.93)
- [ ] Try changing MAX_LEN to 99999, KV_MEM to 4000000000, etc.

## NICE-TO-HAVE (later)
- [ ] Highlight VRAM controls that differ from .env values
- [ ] "Save to .env" button in VRAM tab
- [ ] Export VRAM breakdown as text
- [ ] Compare two configs side-by-side
- [ ] Show token capacity derived from KV pool size
