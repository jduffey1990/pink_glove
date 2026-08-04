import os

bind = "0.0.0.0:8000"
chdir = "/code"

workers = int(os.environ.get("GUNICORN_WORKERS", 4))
threads = int(os.environ.get("GUNICORN_THREADS", 2))
timeout = int(os.environ.get("GUNICORN_TIMEOUT", 60))
graceful_timeout = 30

# Recycle workers periodically so a slow leak can't accumulate indefinitely.
max_requests = 1000
max_requests_jitter = 100

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")

# Trust the platform's proxy headers; SECURE_PROXY_SSL_HEADER handles the rest.
forwarded_allow_ips = "*"
