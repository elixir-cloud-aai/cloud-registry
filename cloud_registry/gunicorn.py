import os

from cloud_registry.app import init_app

# Source application configuration
app_config = init_app().app.config.foca

# Set Gunicorn number of workers and threads
workers = int(os.environ.get('GUNICORN_PROCESSES', '1'))
threads = int(os.environ.get('GUNICORN_THREADS', '1'))

# Set allowed IPs
forwarded_allow_ips = '*'

# Set Gunicorn bind address
bind = '{address}:{port}'.format(
    address=app_config.server.host,
    port=app_config.server.port,
)

# Source environment variables for Gunicorn workers
raw_env = [
    "MONGO_HOST=%s" % os.environ.get('MONGO_HOST', app_config.db.host),
    "MONGO_PORT=%s" % os.environ.get('MONGO_PORT', app_config.db.port),
    "MONGO_DBNAME=%s" % os.environ.get('MONGO_DBNAME', 'taskStore'),
    "MONGO_USERNAME=%s" % os.environ.get('MONGO_USERNAME', ''),
    "MONGO_PASSWORD=%s" % os.environ.get('MONGO_PASSWORD', ''),
]
