# Production Deployment Notes

These deployment files are tuned for a single 8C16G server running Nginx,
Uvicorn, and PostgreSQL on the same machine.

## App service

Install the systemd service after the project has been copied to
`/root/ai-agent-platform` and the virtualenv has been created:

```bash
install -m 0644 deploy/ai-agent-platform.service /etc/systemd/system/ai-agent-platform.service
systemctl daemon-reload
systemctl enable --now ai-agent-platform
```

Defaults:

- `UVICORN_WORKERS=4`
- `UVICORN_LIMIT_CONCURRENCY=200` per worker
- `DB_POOL_SIZE=5`
- `DB_MAX_OVERFLOW=10`

With 4 workers, the app can open up to 60 PostgreSQL connections from the
SQLAlchemy pools. Override these values in `/root/ai-agent-platform/.env` if the
new server is smaller or larger.

## Nginx

Install the site config after DNS and certificates are ready:

```bash
install -m 0644 deploy/nginx-ai-agent-platform.conf /etc/nginx/sites-available/ai-agent-platform.conf
ln -sf /etc/nginx/sites-available/ai-agent-platform.conf /etc/nginx/sites-enabled/ai-agent-platform.conf
nginx -t
systemctl reload nginx
```

The checked-in Nginx file references the current certificate paths. If the new
server uses different certificate names, update the `ssl_certificate` and
`ssl_certificate_key` paths before reloading Nginx.

## PostgreSQL

For PostgreSQL 16 on an 8C16G single-server deployment:

```bash
install -m 0644 deploy/postgresql-8c16g.conf /etc/postgresql/16/main/conf.d/ai-agent-platform.conf
systemctl restart postgresql
```

If the new server uses a different PostgreSQL major version, change `16` in the
target path to the installed version.
