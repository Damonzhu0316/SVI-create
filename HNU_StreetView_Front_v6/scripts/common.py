import csv, io, json, os, hashlib
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
def save(path, obj):
    atomic(path, json.dumps(obj, ensure_ascii=False, indent=2).encode('utf-8'))
def atomic(path, content):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp'); tmp.write_bytes(content); os.replace(tmp, path)
def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def rows(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as f: return list(csv.DictReader(f))
def csvwrite(path, values, fields=None):
    fields = fields or list(dict.fromkeys(k for r in values for k in r))
    buf = io.StringIO(newline=''); w = csv.DictWriter(buf, fieldnames=fields)
    w.writeheader(); w.writerows(values); atomic(path, buf.getvalue().encode('utf-8-sig'))
def sha(path):
    with Path(path).open('rb') as f: return hashlib.file_digest(f, 'sha256').hexdigest()
