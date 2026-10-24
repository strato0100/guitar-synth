#!/usr/bin/env bash
# Compila e instala sfizz_jack (el cliente JACK de sfizz) en /usr/local/bin.
#
# sfizz no está en los repositorios de Raspbian 11, así que se compila desde el
# código fuente de la versión 1.2.3. En una Raspberry Pi 4 tarda entre 15 y 30
# minutos. Sólo hace falta hacerlo una vez.
#
# Uso:
#     bash app/tools/install-sfizz.sh
#     JOBS=3 bash app/tools/install-sfizz.sh      # más núcleos (más RAM y más rápido)
#
# La compilación se guarda en ~/.cache/guitar-synth/sfizz-build hasta que todo
# termina bien. Si algo falla, al volver a ejecutar el script no se repite lo ya
# compilado. Los avisos de GCC sobre "parameter passing ... changed in GCC 7.1"
# son inofensivos.
#
# Se pide la contraseña de sudo para instalar paquetes y copiar el programa.

set -euo pipefail

VERSION="1.2.3"
URL="https://github.com/sfztools/sfizz/releases/download/${VERSION}/sfizz-${VERSION}.tar.gz"
SHA256="a9339eac7620d7f0f6b44bdfe860680fab73e66efad4b5f15b21198dd9436822"
JOBS="${JOBS:-2}"

if command -v sfizz_jack >/dev/null 2>&1; then
    echo "sfizz_jack ya está instalado en $(command -v sfizz_jack)"
    exit 0
fi

echo "==> Instalando herramientas de compilación"
sudo apt-get update
sudo apt-get install -y build-essential cmake pkg-config libjack-jackd2-dev wget

WORK="${XDG_CACHE_HOME:-$HOME/.cache}/guitar-synth/sfizz-build"
mkdir -p "$WORK"
cd "$WORK"

if ! echo "${SHA256}  sfizz.tar.gz" | sha256sum -c - >/dev/null 2>&1; then
    echo "==> Descargando sfizz ${VERSION}"
    wget -q --show-progress -O sfizz.tar.gz "$URL"
    echo "${SHA256}  sfizz.tar.gz" | sha256sum -c -
fi

if [ ! -f src/CMakeLists.txt ]; then
    rm -rf src
    mkdir src
    tar xzf sfizz.tar.gz -C src --strip-components=1
fi

echo "==> Compilando con ${JOBS} núcleo(s) (paciencia, tarda un buen rato)"
# - Sin LTO ni render ni biblioteca compartida: menos tiempo y menos RAM.
# - SFIZZ_GIT_SUBMODULE_CHECK=OFF: el tarball no trae .git y la comprobación fallaría.
# - -Wno-psabi: silencia avisos de GCC que no afectan al resultado.
cmake -S src -B build \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_CXX_FLAGS="-Wno-psabi" \
    -DENABLE_LTO=OFF \
    -DSFIZZ_JACK=ON \
    -DSFIZZ_RENDER=OFF \
    -DSFIZZ_SHARED=OFF \
    -DSFIZZ_TESTS=OFF \
    -DSFIZZ_GIT_SUBMODULE_CHECK=OFF
cmake --build build --target sfizz_jack -j "$JOBS"

# CMake deja el programa en build/library/bin; se busca para no depender de la ruta.
BINARY="$(find build -name sfizz_jack -type f -perm -u+x | head -n 1)"
if [ -z "$BINARY" ]; then
    echo "ERROR: la compilación terminó pero no se encuentra sfizz_jack en ${WORK}/build" >&2
    exit 1
fi

echo "==> Instalando ${BINARY} en /usr/local/bin"
sudo install -m 755 "$BINARY" /usr/local/bin/sfizz_jack

cd "$HOME"
rm -rf "$WORK"

echo
echo "Listo: $(command -v sfizz_jack)"
echo "Reinicia la app (python3 app/run.py): el botón sfizz ya debería estar activo."
