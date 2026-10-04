import json, struct, os, sys
ROOT = r"G:\Apps\HyperQwen\models"
DT = {'F64':8,'F32':4,'F16':2,'BF16':2,'I64':8,'I32':4,'I16':2,'I8':1,'U8':1,'BOOL':1}
def scan(d):
    tot = 0; per = {}; files = 0
    for f in sorted(os.listdir(d)):
        if not f.endswith('.safetensors') or '.bak' in f:
            continue
        p = os.path.join(d, f); files += 1
        with open(p, 'rb') as fh:
            n = struct.unpack('<Q', fh.read(8))[0]
            hdr = json.loads(fh.read(n))
        for k, v in hdr.items():
            if k == '__metadata__':
                continue
            sz = DT[v['dtype']]
            for s in v['shape']:
                sz *= s
            tot += sz
            if k.startswith('model.visual'): c = 'vision'
            elif k.startswith('mtp.'): c = 'mtp'
            elif k == 'lm_head': c = 'lm_head'
            elif k.endswith('embed_tokens'): c = 'embed_tokens'
            elif k.startswith('model.language_model.layers') or k.startswith('model.layers'): c = 'lm_layers'
            else: c = 'other:' + k
            per[c] = per.get(c, 0) + sz
    return tot, per, files
for d in ["Qwen3.8-27B-W4A16-AutoRound", "Qwen3.8-27B-W4A16-AutoRound-fast", "Qwen3.8-27B-DFlash2-W4A16"]:
    p = os.path.join(ROOT, d)
    try:
        tot, per, files = scan(p)
        print('==', d, f'files={files} total={tot/2**30:.4f} GiB ({tot/1e9:.3f} GB)')
        for k, v in sorted(per.items(), key=lambda x: -x[1]):
            print(f'   {k:14s} {v/2**30:8.4f} GiB  {v/2**20:10.1f} MiB')
    except Exception as e:
        print('FAIL', d, type(e).__name__, e)
# tokenizer sizes
for d in ["Qwen3.8-27B-W4A16-AutoRound", "Qwen3.8-27B-DFlash2-W4A16"]:
    p = os.path.join(ROOT, d, 'tokenizer.json')
    if os.path.exists(p):
        print('tokenizer.json', d, f'{os.path.getsize(p)/2**20:.1f} MiB')
