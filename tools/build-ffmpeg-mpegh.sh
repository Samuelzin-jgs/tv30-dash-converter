#!/usr/bin/env bash
# Rode no terminal "MSYS2 UCRT64" (C:\msys64\ucrt64.exe):
#   bash tools/build-ffmpeg-mpegh.sh
# Precisa de ~4 GB livres em C:. Resultado: C:\ffmpeg-mpegh\bin\ffmpeg.exe
set -euo pipefail

PREFIX=/c/ffmpeg-mpegh
SRC=$HOME/ffbuild
JOBS=$(nproc)
mkdir -p "$SRC" "$PREFIX"
export PKG_CONFIG_PATH="$PREFIX/share/pkgconfig:$PREFIX/lib/pkgconfig:/ucrt64/lib/pkgconfig"

pacman -S --needed --noconfirm git make diffutils \
  mingw-w64-ucrt-x86_64-{gcc,cmake,ninja,nasm,pkgconf,python,svt-av1,libxml2,zlib,dav1d,opus,lame,x264,x265}

cd "$SRC"

[ -d mpeghdec ] || git clone --depth 1 https://github.com/Fraunhofer-IIS/mpeghdec.git
cmake -S mpeghdec -B mpeghdec/build -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="$PREFIX" -DCMAKE_CXX_FLAGS="-include share.h"
cmake --build mpeghdec/build -j"$JOBS"
cmake --install mpeghdec/build
sed -i -e 's#^Cflags: .*#Cflags: -I"${includedir}" -DMPEGHDEC_STATIC#' -e 's#^Libs: .*#Libs: -L"${libdir}" -lmpeghdec -lmmtisobmff -lilo -lstdc++ -lm#' "$PREFIX/share/pkgconfig/mpeghdec.pc"

if ! pkg-config --exists lcevc_dec; then
  [ -d LCEVCdec ] || git clone --depth 1 https://github.com/v-novaltd/LCEVCdec.git
  cmake -S LCEVCdec -B LCEVCdec/build -G Ninja -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" -DBUILD_SHARED_LIBS=ON -DCMAKE_C_FLAGS="-mavx2 -mfma" -DCMAKE_CXX_FLAGS="-mavx2 -mfma" \
    -DVN_SDK_UNIT_TESTS=OFF -DVN_SDK_SAMPLE_SOURCE=OFF -DVN_SDK_JSON_CONFIG=OFF \
    -DVN_SDK_EXECUTABLES=OFF -DVN_SDK_DOCS=OFF
  cmake --build LCEVCdec/build -j"$JOBS"
  cmake --install LCEVCdec/build
fi

[ -d FFmpeg ] || git clone --depth 1 https://github.com/FFmpeg/FFmpeg.git
cd FFmpeg
./configure --prefix="$PREFIX" --arch=x86_64 --target-os=mingw32 \
  --enable-gpl --enable-version3 --enable-nonfree \
  --enable-libmpeghdec --enable-liblcevc-dec --enable-libsvtav1 \
  --enable-libxml2 --enable-libdav1d --enable-libopus --enable-libmp3lame \
  --enable-libx264 --enable-libx265 \
  --extra-cflags="-I$PREFIX/include" --extra-ldflags="-L$PREFIX/lib"
make -j"$JOBS"
make install

cp /ucrt64/bin/*.dll "$PREFIX/bin/" 2>/dev/null || true
cp -n "$PREFIX/bin/liblcevc_dec_pipeline_cpu.dll" "$PREFIX/bin/lcevc_dec_pipeline_cpu.dll" 2>/dev/null || true

"$PREFIX/bin/ffmpeg.exe" -hide_banner -decoders | grep -Ei "mpegh|lcevc" || true
"$PREFIX/bin/ffmpeg.exe" -hide_banner -filters | grep -i lcevc || true
