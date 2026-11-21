import os
import secrets
import tempfile

from cryptography.fernet import Fernet

# Isolate before importing settings: local_secret() otherwise reads the
# operator's signing/encryption keys, and settings may capture real API keys.
os.environ['DJANGO_SECRET_KEY'] = secrets.token_urlsafe(64)
os.environ['MFA_ENCRYPTION_KEY'] = Fernet.generate_key().decode('ascii')
os.environ['ROUNDTABLE_IGNORE_LOCAL_MAIL'] = '1'
for _key in ('ROUNDTABLE_AI_PROVIDER', 'ROUNDTABLE_AI_MODEL', 'ROUNDTABLE_SERVICE_REGION',
             'ROUNDTABLE_AI_REASONING_EFFORT', 'DEEPSEEK_API_KEY', 'KIMI_API_KEY',
             'QWEN_API_KEY', 'QWENCLOUD_API_KEY', 'DASHSCOPE_API_KEY', 'AI_GATEWAY_API_KEY', 'BRAVE_SEARCH_API_KEY'):
    os.environ.pop(_key, None)

from .settings import *

PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'

# Tests must never use the operator's saved connection or inherited real API keys.
_test_temp_root = BASE_DIR / 'work' / 'test-tmp'
_test_temp_root.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(_test_temp_root)
_connection_directory = tempfile.TemporaryDirectory(prefix='roundtable-test-connection-', dir=_test_temp_root)
PROVIDER_CONNECTION_PATH = Path(_connection_directory.name) / 'provider-connection.json'
NAMED_CONNECTIONS_DIR = Path(_connection_directory.name) / 'named-connections'
TEAM_ROUTING_PATH = Path(_connection_directory.name) / 'team-routing.json'
LOCAL_WORKER_LOCK_PATH = Path(_connection_directory.name) / 'local-worker.lock'
AI_MODEL = ''
AI_GATEWAY_API_KEY = ''
EXECUTION_ENABLED = False
EXECUTION_IMAGES = {}
