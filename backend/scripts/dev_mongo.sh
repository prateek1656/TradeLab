#!/usr/bin/env bash
# Start a local single-node MongoDB replica set (rs0 on 127.0.0.1:27017) for the backend tests, without Docker.
# Works in a fresh cloud container: MongoDB comes from conda-forge (reachable through the session proxy) and is
# unpacked with Python. Usage: backend/scripts/dev_mongo.sh [DIR]   (default DIR: ~/.tradelab-mongo)
set -euo pipefail
DIR="${1:-$HOME/.tradelab-mongo}"
PY="${PYTHON:-python3}"
URL="https://conda.anaconda.org/conda-forge/linux-64/mongodb-8.0.23-h8ca7601_0.conda"
mkdir -p "$DIR/data" "$DIR/log"
if [ ! -x "$DIR/root/bin/mongod" ]; then
  curl -fsSL "$URL" -o "$DIR/mongodb.conda"
  "$PY" -c "import zstandard" 2>/dev/null || "$PY" -m pip install -q zstandard
  "$PY" - "$DIR" <<'PY'
import io, sys, tarfile, zipfile, zstandard
d = sys.argv[1]
with zipfile.ZipFile(f"{d}/mongodb.conda") as z:
    name = next(n for n in z.namelist() if n.startswith("pkg-") and n.endswith(".tar.zst"))
    raw = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(z.read(name))).read()
tarfile.open(fileobj=io.BytesIO(raw)).extractall(f"{d}/root")
PY
fi
if ! (echo > /dev/tcp/127.0.0.1/27017) 2>/dev/null; then
  rm -f "$DIR/data/mongod.lock"
  "$DIR/root/bin/mongod" --dbpath "$DIR/data" --port 27017 --bind_ip 127.0.0.1 --replSet rs0 --fork \
    --logpath "$DIR/log/mongod.log" --wiredTigerCacheSizeGB 0.5 >/dev/null
fi
"$PY" - <<'PY'
import time
from pymongo import MongoClient
from pymongo.errors import OperationFailure
c = MongoClient("mongodb://127.0.0.1:27017/?directConnection=true", serverSelectionTimeoutMS=20000)
try:
    c.admin.command("replSetGetStatus")
except OperationFailure:
    c.admin.command("replSetInitiate", {"_id": "rs0", "members": [{"_id": 0, "host": "127.0.0.1:27017"}]})
for _ in range(60):
    if c.admin.command("hello").get("isWritablePrimary"):
        print("MongoDB replica set rs0 ready on 127.0.0.1:27017")
        break
    time.sleep(0.5)
else:
    raise SystemExit("MongoDB did not become primary")
PY
