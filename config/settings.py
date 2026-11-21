"""Local internal development settings. Production is an explicit separate configuration."""
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / 'data'
DATA_DIR.mkdir(exist_ok=True)
ENVIRONMENT = os.environ.get('ROUNDTABLE_ENV', 'development')
DEBUG = ENVIRONMENT == 'development'


def local_secret(name, generate):
    value = os.environ.get(name)
    if value:
        return value
    if not DEBUG:
        raise ImproperlyConfigured(f'{name} is required outside local development.')
    target = DATA_DIR / (name.lower() + '.key')
    try:
        with target.open('x', encoding='utf-8') as stream:
            stream.write(generate())
    except FileExistsError:
        pass
    return target.read_text(encoding='utf-8').strip()


SECRET_KEY = local_secret('DJANGO_SECRET_KEY', lambda: secrets.token_urlsafe(64))
MFA_ENCRYPTION_KEY = local_secret('MFA_ENCRYPTION_KEY', lambda: Fernet.generate_key().decode())
ALLOWED_HOSTS = os.environ.get('DJANGO_ALLOWED_HOSTS', '127.0.0.1,localhost,[::1]').split(',')
INSTALLED_APPS = [
    'django.contrib.auth', 'django.contrib.contenttypes', 'django.contrib.sessions',
    'django.contrib.messages', 'django.contrib.staticfiles',
    'accounts', 'workspace', 'delivery',
]
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'config.middleware.BrowserSafetyMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'accounts.middleware.MFARequiredMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]
ROOT_URLCONF = 'config.urls'
TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'DIRS': [BASE_DIR / 'templates'], 'APP_DIRS': True,
    'OPTIONS': {'context_processors': [
        'django.template.context_processors.request',
        'django.contrib.auth.context_processors.auth',
        'django.contrib.messages.context_processors.messages',
    ]},
}]
WSGI_APPLICATION = 'config.wsgi.application'
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': DATA_DIR / 'roundtable.sqlite3', 'OPTIONS': {'timeout': 20}}}
AUTH_USER_MODEL = 'accounts.User'
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 12}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]
LANGUAGE_CODE = 'zh-hans'
TIME_ZONE = 'Asia/Shanghai'
USE_I18N = True
USE_TZ = True
STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_AGE = 60 * 60 * 24 * 7
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_SSL_REDIRECT = not DEBUG
SECURE_HSTS_SECONDS = 0 if DEBUG else 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'
LOGIN_URL = '/'
DATA_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024
FILE_UPLOAD_HANDLERS = ['workspace.source_parsers.BoundedMemoryUploadHandler']
DATA_UPLOAD_MAX_NUMBER_FILES = 1
EMAIL_DELIVERY_ENABLED = os.environ.get('EMAIL_DELIVERY_ENABLED') == '1'
EMAIL_BACKEND = os.environ.get('EMAIL_BACKEND', 'django.core.mail.backends.locmem.EmailBackend')
EMAIL_HOST = os.environ.get('EMAIL_HOST', '')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
EMAIL_USE_TLS = True
EMAIL_USE_SSL = False
EMAIL_TIMEOUT = 15
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', 'roundtable@localhost')
PUBLIC_BASE_URL = os.environ.get('PUBLIC_BASE_URL', 'http://127.0.0.1:8910')
LOCAL_EMAIL_RECOVERY_ENABLED = False
# The personal-computer wizard is deliberately ignored in tests and production.
# Explicit environment configuration always wins over the optional local file.
LOCAL_EMAIL_PATH = DATA_DIR / 'email-connection.bin'
LOCAL_WORKER_LOCK_PATH = DATA_DIR / 'local-worker.lock'
LOCAL_MAIL_CONFIG_ENABLED = (DEBUG and os.environ.get('ROUNDTABLE_IGNORE_LOCAL_MAIL') != '1' and
                             not any(name in os.environ for name in ('EMAIL_DELIVERY_ENABLED', 'EMAIL_BACKEND', 'EMAIL_HOST',
                                                                     'EMAIL_HOST_USER', 'EMAIL_HOST_PASSWORD')))
# Empty model selection intentionally keeps AI unavailable until an explicit verified integration.
AI_MODEL = os.environ.get('ROUNDTABLE_AI_MODEL', '')
AI_GATEWAY_API_KEY = os.environ.get('AI_GATEWAY_API_KEY', '')
SERVICE_REGION = os.environ.get('ROUNDTABLE_SERVICE_REGION', 'local-development')
RELEASE_STAGE = 'internal'
# Experimental structured-length transport stays off until its real provider
# behavior is verified and explicitly approved for the live service.
QWEN_STRUCTURED_HANZI_ENABLED = False
# No image is downloaded and no generated code runs without this explicit local configuration.
EXECUTION_ENABLED = os.environ.get('ROUNDTABLE_EXECUTION_ENABLED') == '1'
EXECUTION_IMAGES = {tool: image for tool, image in {
    'python_unittest': os.environ.get('ROUNDTABLE_EXECUTION_PYTHON_IMAGE', ''),
    'javascript_check': os.environ.get('ROUNDTABLE_EXECUTION_JAVASCRIPT_IMAGE', ''),
    'python_stdio': os.environ.get('ROUNDTABLE_EXECUTION_STDIO_IMAGE', ''),
}.items() if image}
