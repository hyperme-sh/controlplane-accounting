#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source_root="$PWD"
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
uv build "$source_root" --out-dir "$scratch/dist"
uv venv --python 3.13 "$scratch/venv"
uv pip install --python "$scratch/venv/bin/python" "$scratch"/dist/*.whl pytest pytest-asyncio httpx
cp -R "$source_root/tests" "$scratch/tests"
(cd "$scratch" && PYTHONDONTWRITEBYTECODE=1 "$scratch/venv/bin/python" -I -m pytest -q -o asyncio_mode=auto tests)
python3 - "$scratch" <<'PY'
import pathlib,sys,tarfile
root=pathlib.Path(sys.argv[1])
with tarfile.open(next((root/'dist').glob('*.tar.gz'))) as t:t.extractall(root/'rebuilt',filter='data')
PY
uv build --wheel "$scratch"/rebuilt/* --out-dir "$scratch/wheels"
uv pip install --python "$scratch/venv/bin/python" --reinstall "$scratch"/wheels/*.whl
(cd "$scratch" && PYTHONDONTWRITEBYTECODE=1 "$scratch/venv/bin/python" -I -m pytest -q -o asyncio_mode=auto tests)
