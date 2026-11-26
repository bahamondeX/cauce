FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && chmod 0755 /usr/sbin/policy-rc.d \
    && apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       postfix=3.7.11-0+deb12u1 dovecot-core=1:2.3.19.1+dfsg1-2.1+deb12u7 \
       dovecot-imapd=1:2.3.19.1+dfsg1-2.1+deb12u7 dovecot-pop3d=1:2.3.19.1+dfsg1-2.1+deb12u7 \
    && rm -rf /var/lib/apt/lists/*
COPY dovecot.conf /fixture/dovecot.conf
COPY start.sh /fixture/start.sh
CMD ["sh", "/fixture/start.sh"]
