#!/bin/sh
set -eu

dest=${1:-"$(dirname "$0")/certs"}
mkdir -p "$dest"
umask 022
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$dest/ca.key" -out "$dest/ca.crt" \
  -days 3 -sha256 -subj '/CN=Cauce Increment 8 Test CA' \
  -addext 'basicConstraints=critical,CA:TRUE' \
  -addext 'keyUsage=critical,keyCertSign,cRLSign'

issue() {
  name=$1
  san=$2
  openssl req -new -newkey rsa:2048 -nodes -keyout "$dest/$name.key" -out "$dest/$name.csr" \
    -subj "/CN=$san"
  cat > "$dest/$name.ext" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:$san
authorityKeyIdentifier=keyid,issuer
EOF
  openssl x509 -req -in "$dest/$name.csr" -CA "$dest/ca.crt" -CAkey "$dest/ca.key" \
    -CAcreateserial -out "$dest/$name.crt" -days 3 -sha256 -extfile "$dest/$name.ext"
  rm -f "$dest/$name.csr" "$dest/$name.ext"
  chmod 644 "$dest/$name.key" "$dest/$name.crt"
}

issue proxy proxy
issue backend backend.test
issue grpc backend.test
chmod 644 "$dest/ca.crt"
printf 'Generated ephemeral test certificates under %s\n' "$dest"
