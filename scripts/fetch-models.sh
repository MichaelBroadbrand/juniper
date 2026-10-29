#!/bin/sh
# Downloads the two small models Juniper runs on your Mac into models/ (about 60 MB), from the
# sherpa-onnx project's releases, and checks them against known hashes:
#   - the wake-word spotter (sherpa-onnx keyword spotting, zipformer, Chinese + English, 3M)
#   - the speaker-verification model that tells your voice from others (WeSpeaker, VoxCeleb)
# Speech recognition itself is Apple's (macOS 26) or Whisper, which downloads on first use.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
M="$ROOT/models"
KWS=sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20
BASE=https://github.com/k2-fsa/sherpa-onnx/releases/download
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$M/$KWS" "$M/speaker"
if [ ! -f "$M/$KWS/tokens.txt" ]; then
  echo "› wake-word model"
  curl -fL# -o "$TMP/kws.tar.bz2" "$BASE/kws-models/$KWS.tar.bz2"
  tar -xjf "$TMP/kws.tar.bz2" -C "$TMP"
  for f in tokens.txt en.phone encoder-epoch-13-avg-2-chunk-16-left-64.int8.onnx \
           decoder-epoch-13-avg-2-chunk-16-left-64.onnx joiner-epoch-13-avg-2-chunk-16-left-64.int8.onnx; do
    cp "$TMP/$KWS/$f" "$M/$KWS/"
  done
fi
if [ ! -f "$M/speaker/wespeaker_en_voxceleb_resnet34_LM.onnx" ]; then
  echo "› speaker model"
  curl -fL# -o "$M/speaker/wespeaker_en_voxceleb_resnet34_LM.onnx" \
    "$BASE/speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx"
fi

cd "$M"
shasum -a 256 -c <<'SUMS'
e9848563da86f263117134dfd7ad63c92355b37de492b55e325400c9d9c39012  speaker/wespeaker_en_voxceleb_resnet34_LM.onnx
2d3f32311f9b692b964da3c90e830258d3e78e013cb0c992dbfb15cd5a1a71b0  sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20/tokens.txt
f7000ec3a90544c0c7c16090d8951779c2b322e14dad5006290f498567d439ea  sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20/en.phone
408bbd740838c42d5bf6d1c5b80b3c88b616c7860b92d980328b5b068c76ae48  sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20/encoder-epoch-13-avg-2-chunk-16-left-64.int8.onnx
63a22dd60f40fff082ac3e09afa507f6787da36df76ded2fbe145fa233e22c21  sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20/decoder-epoch-13-avg-2-chunk-16-left-64.onnx
190d4067b4cc20b72a42a1916e69d92052000fb7051a427ebb1bc72a69207dc1  sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20/joiner-epoch-13-avg-2-chunk-16-left-64.int8.onnx
SUMS
echo "✓ models ready in $M"
