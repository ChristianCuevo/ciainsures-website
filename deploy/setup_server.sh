#!/bin/bash
# One-time (idempotent) server setup for ciainsures.com on Ubuntu 24.04. Run as root:
#   DEPLOY_PUBKEY="ssh-ed25519 AAAA... name" bash setup_server.sh
set -euo pipefail
APP=/srv/ciainsures
HERE=$(cd "$(dirname "$0")" && pwd)

echo "==> Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q nginx git python3 python3-jinja2 python3-bs4 python3-lxml python3-pil \
    certbot python3-certbot-nginx unattended-upgrades

echo "==> Users"
id deploy >/dev/null 2>&1 || useradd --create-home --shell /bin/bash deploy
id ciaapi >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin ciaapi
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
if [ -n "${DEPLOY_PUBKEY:-}" ]; then
  touch /home/deploy/.ssh/authorized_keys
  grep -qxF "$DEPLOY_PUBKEY" /home/deploy/.ssh/authorized_keys || echo "$DEPLOY_PUBKEY" >> /home/deploy/.ssh/authorized_keys
  chown deploy:deploy /home/deploy/.ssh/authorized_keys; chmod 600 /home/deploy/.ssh/authorized_keys
fi
cat > /etc/sudoers.d/ciainsures-deploy <<'EOF'
deploy ALL=(root) NOPASSWD: /usr/bin/systemctl restart ciainsures-api, /usr/bin/systemctl reload nginx, /usr/sbin/nginx -t -q
EOF
chmod 440 /etc/sudoers.d/ciainsures-deploy
visudo -cf /etc/sudoers.d/ciainsures-deploy

echo "==> Directories + git repo"
install -d -o deploy -g deploy "$APP" "$APP/src" "$APP/src/deploy" "$APP/releases"
[ -f "$APP/src/deploy/redirects.map" ] || sudo -u deploy touch "$APP/src/deploy/redirects.map"
if [ ! -d "$APP/repo.git" ]; then
  sudo -u deploy git init -q --bare -b main "$APP/repo.git"
fi
install -m 755 -o deploy -g deploy "$HERE/post-receive" "$APP/repo.git/hooks/post-receive"
if [ ! -e "$APP/current" ]; then
  install -d -o deploy -g deploy "$APP/releases/placeholder"
  echo '<!doctype html><title>Central Insurance Agency</title><p>Site is being deployed…</p>' > "$APP/releases/placeholder/index.html"
  sudo -u deploy ln -sfn "$APP/releases/placeholder" "$APP/current"
fi

echo "==> API service"
install -d -m 750 -o root -g ciaapi /etc/ciainsures
if [ ! -f /etc/ciainsures/api.env ]; then
  PW=$(python3 -c 'import secrets;print(secrets.token_urlsafe(18))')
  cat > /etc/ciainsures/api.env <<EOF
ADMIN_PASSWORD=$PW
SITE_URL=https://ciainsures.com
# Optional e-mail alerts (e.g. Microsoft 365: smtp.office365.com, port 587, a licensed mailbox with SMTP AUTH enabled)
#SMTP_HOST=smtp.office365.com
#SMTP_PORT=587
#SMTP_USER=website@ciainsures.com
#SMTP_PASS=
#SMTP_FROM=website@ciainsures.com
#NOTIFY_TO=contactus@ciainsures.com
EOF
  echo "ADMIN PASSWORD (save this): $PW"
fi
chown root:ciaapi /etc/ciainsures/api.env; chmod 640 /etc/ciainsures/api.env
install -m 644 "$HERE/ciainsures-api.service" /etc/systemd/system/ciainsures-api.service
systemctl daemon-reload
systemctl enable ciainsures-api >/dev/null

echo "==> nginx"
install -m 644 "$HERE/cia-headers.conf" /etc/nginx/snippets/cia-headers.conf
install -m 644 "$HERE/nginx-ciainsures.conf" /etc/nginx/sites-available/ciainsures
ln -sfn /etc/nginx/sites-available/ciainsures /etc/nginx/sites-enabled/ciainsures
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable --now nginx >/dev/null
systemctl reload nginx

echo "==> Automatic security updates"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

echo "==> Done"
