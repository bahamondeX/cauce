#!/bin/sh
set -eu
mkdir -p /fixture/mail/cur /fixture/mail/new /fixture/mail/tmp
chown -R 1001:1001 /fixture/mail
printf 'fixture:{PLAIN}test-only-password\n' > /fixture/passwd
chmod 0644 /fixture/passwd
postconf -e 'myhostname=fixture.test' 'inet_interfaces=all' 'inet_protocols=ipv4' \
    'smtpd_sasl_auth_enable=yes' 'smtpd_sasl_type=dovecot' 'smtpd_sasl_path=private/auth' \
    'smtpd_relay_restrictions=permit_sasl_authenticated,reject' 'maillog_file=/dev/stdout'
dovecot -c /fixture/dovecot.conf
exec postfix start-fg
