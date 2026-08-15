#!/bin/zsh
# Rebuild both README GIFs end to end, from the REAL renderer.
#   ./build_gifs.sh            both
#   ./build_gifs.sh monthly    just the hero
set -e
set -o pipefail          # a generator that dies must abort the build, not be
                         # swallowed by the `tail` it is piped into
SP="${0:A:h}"
REPO="${SP:h:h}"
PY="${PYTHON:-$REPO/venv/bin/python}"
ASSETS="$REPO/docs/assets"
cd "$SP"

build() {                      # build <generator.py> <output-name>
  local gen="$1" name="$2"
  echo "── $name ─────────────────────────────────────────"
  "$PY" "$gen"          2>/dev/null | tail -2
  "$PY" render_html.py
  node capture.js
  # 92 captured frames -> 10fps, 760px wide (what the README embeds).
  # 64 colours + lossy=80 was swept against 5 configs: it is the knee of the
  # curve, ~1.2MB with the body text still crisp. Scrolling text defeats
  # frame differencing, so there is no cheap win below this.
  ffmpeg -loglevel error -y -framerate 24 -i frames/f%04d.png \
    -vf "fps=10,scale=760:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=64[p];[b][p]paletteuse=dither=bayer:bayer_scale=3" \
    "$name.raw.gif"
  gifsicle -O3 --lossy=80 --colors 64 "$name.raw.gif" -o "$ASSETS/$name.gif"
  rm -f "$name.raw.gif"
  echo "→ $ASSETS/$name.gif  $(( $(stat -f%z "$ASSETS/$name.gif") / 1024 ))K  $(gifsicle --info "$ASSETS/$name.gif" | head -1 | sed 's/.*gif //')"
}

[[ "$1" == "danger"  ]] || build make_comment.py monthly-bump
[[ "$1" == "monthly" ]] || build make_danger.py  danger-blocked
