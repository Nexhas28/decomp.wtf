# decomp.wtf in Docker: how it runs on macOS (install.sh does all of this for you), and on servers if you like.
# The same Linux as decomp.wtf's WSL system on Windows (linux-base.sh). decomp.wtf itself installs into the
# decomp user's home on the first start: keep /home/decomp on a volume, and updates it installs itself stay.
#
#   docker build -t decomp-wtf .         (in this folder: Dockerfile, linux-base.sh, install.sh)
#   docker run -d --name decomp-wtf -p 127.0.0.1:6464:6464 -e DECOMP_PORT=6464 \
#     -v decomp-wtf-home:/home/decomp decomp-wtf
#   docker exec decomp-wtf decomp url    (the address that opens it, signed in)
#
# The compilers inside are x86 Linux programs: on Apple Silicon this runs as linux/amd64 (Rosetta or QEMU).
FROM --platform=linux/amd64 debian:trixie-slim

COPY linux-base.sh /tmp/linux-base.sh
RUN sh /tmp/linux-base.sh docker && rm /tmp/linux-base.sh
COPY install.sh /usr/local/share/decomp-wtf/install.sh

USER decomp
WORKDIR /home/decomp
ENV LANG=C.UTF-8 DECOMP_RUNTIME=docker PATH=/home/decomp/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
EXPOSE 6464
HEALTHCHECK --interval=30s --timeout=5s --start-period=10m CMD curl -fsS "http://127.0.0.1:${DECOMP_PORT:-6464}/health" >/dev/null || exit 1
ENTRYPOINT ["/usr/local/bin/decomp-entrypoint"]
