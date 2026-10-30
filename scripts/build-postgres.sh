#!/usr/bin/env bash
set -euo pipefail

PG_VERSION=18.6
LZ4_VERSION=1.10.0
PG_BIGM_TAG=v1.2-20250903
LZ4_SHA256=537512904744b35e232912055ccf8ec66d768639ff3abe5788d90d792ec5f48b
PG_BIGM_SHA256=4d4fb48161e3f9e99207851299379c83b3e5d1d3c1a07c35f6a3153d577b3142
M4_VERSION=1.4.19
M4_SHA256=63aede5c6d33b6d9b13511cd0be2cac046f2e70fd0a07aa9573a04a82783af96
BISON_VERSION=3.8.2
BISON_SHA256=9bba0214ccf7f1079c5d59210045227bcf619519840ebfa80cd3849cff5a5bf2
FLEX_VERSION=2.6.4
FLEX_SHA256=e87aae032bf07c26f85ac0ed3250998c37621d95f8bd748b31f15b33c45ee995

mkdir -p "$1"
prefix=$(cd "$1" && pwd)
build_dir=$(mktemp -d)
trap 'rm -rf "$build_dir"' EXIT
cd "$build_dir"

curl -fL -O "https://ftp.postgresql.org/pub/source/v${PG_VERSION}/postgresql-${PG_VERSION}.tar.gz"
curl -fL -O "https://ftp.postgresql.org/pub/source/v${PG_VERSION}/postgresql-${PG_VERSION}.tar.gz.sha256"
sha256sum -c "postgresql-${PG_VERSION}.tar.gz.sha256"
curl -fL -o lz4.tar.gz "https://github.com/lz4/lz4/releases/download/v${LZ4_VERSION}/lz4-${LZ4_VERSION}.tar.gz"
printf '%s  %s\n' "$LZ4_SHA256" lz4.tar.gz | sha256sum -c -
curl -fL -o pg_bigm.tar.gz "https://github.com/pgbigm/pg_bigm/archive/refs/tags/${PG_BIGM_TAG}.tar.gz"
printf '%s  %s\n' "$PG_BIGM_SHA256" pg_bigm.tar.gz | sha256sum -c -

# PostgreSQL 发行包不含生成好的解析器，需要 bison 和 flex；缺少时从源码构建。
build_tool() {
    curl -fL -o "$1.tar" "$3"
    printf '%s  %s\n' "$2" "$1.tar" | sha256sum -c -
    mkdir "$1-src"
    tar xf "$1.tar" -C "$1-src" --strip-components=1
    (cd "$1-src" && ./configure --prefix="$build_dir/tools" && make -j"$(nproc)" && make install)
}
export PATH="$build_dir/tools/bin:$PATH"
command -v m4 || build_tool m4 "$M4_SHA256" "https://ftp.gnu.org/gnu/m4/m4-${M4_VERSION}.tar.xz"
command -v bison || build_tool bison "$BISON_SHA256" "https://ftp.gnu.org/gnu/bison/bison-${BISON_VERSION}.tar.xz"
command -v flex || build_tool flex "$FLEX_SHA256" "https://github.com/westes/flex/releases/download/v${FLEX_VERSION}/flex-${FLEX_VERSION}.tar.gz"

tar xf lz4.tar.gz
make -C "lz4-${LZ4_VERSION}/lib" -j"$(nproc)" BUILD_SHARED=no CFLAGS=-fPIC
make -C "lz4-${LZ4_VERSION}/lib" install BUILD_SHARED=no CFLAGS=-fPIC \
    PREFIX="$build_dir/lz4" libdir="$build_dir/lz4/lib"

tar xf "postgresql-${PG_VERSION}.tar.gz"
cd "postgresql-${PG_VERSION}"
LZ4_CFLAGS="-I$build_dir/lz4/include" LZ4_LIBS="-L$build_dir/lz4/lib -llz4" \
    CPPFLAGS="-I$build_dir/lz4/include" LDFLAGS="-L$build_dir/lz4/lib" \
    ./configure --prefix="$prefix" --with-lz4 --without-readline --without-icu --without-zlib
make -j"$(nproc)" world-bin
make install-world-bin

cd "$build_dir"
tar xf pg_bigm.tar.gz
cd "pg_bigm-${PG_BIGM_TAG#v}"
make -j"$(nproc)" USE_PGXS=1 PG_CONFIG="$prefix/bin/pg_config"
make install USE_PGXS=1 PG_CONFIG="$prefix/bin/pg_config"
