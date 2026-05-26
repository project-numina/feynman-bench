# FIRE6 (Feynman Integral REduction) build, reproducible across hosts.
#
# Build:
#   docker build --platform linux/amd64 -t fire6 .
#
# Drop into a shell with the binaries (FIRE6, FIRE6p, Ftool6) on PATH:
#   docker run --rm -it --platform linux/amd64 fire6
#
# Run a reduction from your host:
#   docker run --rm --platform linux/amd64 \
#       -v "$PWD/my.config:/work/my.config" \
#       -v "$PWD/output:/output" \
#       fire6 bin/FIRE6 -c /work/my
#
# Pin to linux/amd64. FIRE6 ships fermat (its coefficient calculator) as an
# x86_64 binary only -- on Apple Silicon, Docker runs the image through qemu
# (slower but correct).
FROM --platform=linux/amd64 ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Etc/UTC

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        autoconf \
        automake \
        libtool \
        cmake \
        pkg-config \
        m4 \
        git \
        ca-certificates \
        zlib1g-dev \
        libgmp-dev \
        libmpfr-dev \
        python3 \
        diffutils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /fire

# 1) Bring in everything needed by `make depend` first. This layer is the
#    expensive one; we want it cached as long as the bundled extra/ libs
#    and the build orchestration don't change.
COPY paths.inc.in configure Makefile.in ./
COPY extra ./extra
COPY usr ./usr
COPY poly/Makefile.in ./poly/Makefile.in
COPY prime/Makefile.in ./prime/Makefile.in
COPY tools ./tools
COPY mpi ./mpi

# 2) Strip macOS/host build leftovers that ride along in extra/ and usr/.
#    The host typically built these locally and the .o/.a/.dylib files
#    would break the Linux relink under their existing Makefiles.
#    Exception: extra/fuel/extra/ ships fermat binaries for both macOS
#    and Linux -- we keep those.
RUN find extra usr \
        \( -name '*.o' -o -name '*.lo' -o -name '*.a' \
        -o -name '*.dylib' -o -name '*.so' -o -name '*.so.*' \) \
        ! -path 'extra/fuel/extra/*' \
        -print -delete && \
    rm -rf usr/lib/* usr/lib64/* usr/bin/* usr/share/* && \
    rm -rf usr/include/google usr/include/gperftools && \
    find usr/include -maxdepth 1 -name 'kc*.h' -delete && \
    find usr/include -maxdepth 1 -name 'lz4*.h' -delete && \
    find usr/include -maxdepth 1 -name 'zstd*.h' -delete

# 3) Configure and build dependency libraries. The submodule step of
#    `make depend` calls `cd .. && git submodule update`, which assumes a
#    parent .git that we deliberately do not ship into the image -- so we
#    no-op that target (extra/fuel is already vendored by the host COPY).
#    tcmalloc is left disabled because gperftools 2.7 has a known archive
#    issue on aarch64 build hosts; the rational and modular reductions run
#    fine with the system allocator.
RUN ./configure --enable-zstd --enable-debug && \
    sed -i 's|^	cd \.\. && git submodule update --init --recursive$|	@echo "submodule step skipped: extra/fuel already vendored"|' Makefile && \
    make depend

# 4) Bring in FIRE6's own sources and build the binaries.
COPY sources ./sources
COPY examples ./examples
COPY tests ./tests
COPY bin ./bin
COPY tests.makefile ./

RUN make

ENV PATH=/fire/bin:$PATH
CMD ["bash"]
