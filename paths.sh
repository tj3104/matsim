# paths.env を読む (run.sh から source する)。すでに環境変数にある値 (シェルや .env で決めたもの) は上書きしない。
# ~ と $変数 は展開する。場所は MATSIM_PATHS、なければこのディレクトリの paths.env。なければ何もしない (既定の場所で動く)
matsim_load_paths() {
  local file="${MATSIM_PATHS:-$(dirname "${BASH_SOURCE[0]}")/paths.env}" line key
  [[ -f "$file" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    [[ -z "$line" || "$line" == \#* ]] && continue
    line="${line#export }"
    key="${line%%=*}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ && "$line" == *=* ]] || { echo "paths.env: 読めない行です: $line" >&2; return 1; }
    [[ -n "${!key+x}" ]] && continue
    eval "export $line"
  done < "$file"
}
