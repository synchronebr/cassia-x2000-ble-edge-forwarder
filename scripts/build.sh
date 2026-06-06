#!/usr/bin/env bash
#
# build.sh — empacota um release do sync_reading para um ambiente.
#
# Uso:
#   scripts/build.sh <qa|prd> [versao]
#
# Sem [versao], usa o conteúdo do arquivo VERSION na raiz do repo.
# Gera: dist/sync_reading-<versao>-<env>.tar.gz
#
# Mescla config/config.base.json + config/config.<env>.json no config.json final.
# O segredo (api_key) NÃO entra no pacote — fica em /root/config/sync_reading/config.json
# no próprio gateway e é mesclado em runtime pelo lib/config.py.
#
set -euo pipefail

ENV="${1:-}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${2:-$(cat "$REPO_ROOT/VERSION")}"

case "$ENV" in
  qa|prd) ;;
  *) echo "uso: $0 <qa|prd> [versao]" >&2; exit 2 ;;
esac

APP="sync_reading"
SRC="$REPO_ROOT/src"
CFG_DIR="$REPO_ROOT/config"
PKG_DIR="$REPO_ROOT/packaging"
DIST="$REPO_ROOT/dist"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

TOP="$STAGE/$APP"
APPDIR="$TOP/opt/$APP"
mkdir -p "$APPDIR/lib"

# 1) Fonte (sem lixo)
cp "$SRC/app.py" "$APPDIR/app.py"
cp "$SRC/version.py" "$APPDIR/version.py"
for f in "$SRC"/lib/*.py; do cp "$f" "$APPDIR/lib/"; done

# 2) autorun / delete
cp "$PKG_DIR/autorun.sh" "$TOP/autorun.sh"
cp "$PKG_DIR/delete_app.sh" "$TOP/delete_app.sh"
chmod +x "$TOP/autorun.sh" "$TOP/delete_app.sh"

# 3) Config mesclada (base + overlay do ambiente) -> config.json
python3 - "$CFG_DIR/config.base.json" "$CFG_DIR/config.$ENV.json" "$APPDIR/config.json" <<'PY'
import json, sys
base, overlay, out = sys.argv[1], sys.argv[2], sys.argv[3]
with open(base) as f: cfg = json.load(f)
with open(overlay) as f: cfg.update(json.load(f))
with open(out, "w") as f: json.dump(cfg, f, indent=2, ensure_ascii=False); f.write("\n")
PY

# 4) Carimbo de versão
cat > "$APPDIR/version.py" <<PY
# Gerado por scripts/build.sh — não editar à mão.
__version__ = "$VERSION"
BUILD_ENV = "$ENV"
PY

# 5) Empacota
mkdir -p "$DIST"
OUT="$DIST/${APP}-${VERSION}-${ENV}.tar.gz"
tar -C "$STAGE" \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='.DS_Store' \
  -czf "$OUT" "$APP"

echo "OK  $OUT"
echo "    versão=$VERSION  env=$ENV  cloud_url=$(python3 -c "import json;print(json.load(open('$APPDIR/config.json'))['cloud_url'])")"
