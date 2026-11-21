"""Explicit production settings validation, without importing settings or secrets files."""
import ipaddress
import re
from email.utils import parseaddr
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured


def public_host(value):
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        pass
    labels = value.lower().split('.')
    return (len(value) <= 253 and len(labels) >= 2 and
            labels[-1] not in {'localhost', 'local', 'internal', 'invalid', 'test', 'example'} and
            all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels))


def valid_public_origin(value, hosts):
    try:
        url = urlsplit(value)
        return (url.scheme == 'https' and url.hostname in hosts and public_host(url.hostname) and
                not url.username and not url.password and url.port in (None, 443) and
                url.path in ('', '/') and not url.query and not url.fragment)
    except (TypeError, ValueError):
        return False


def valid_secret(value):
    return isinstance(value, str) and len(value) >= 50 and len(set(value)) >= 8


def valid_fernet(value):
    try:
        Fernet(value)
        return True
    except (ValueError, TypeError):
        return False


def production_values(environ):
    """Return settings only after required inputs pass. Errors name fields, never values."""
    def required(name):
        value = environ.get(name, '')
        if not isinstance(value, str) or not value.strip():
            raise ImproperlyConfigured(f'{name} must be explicitly configured for production.')
        return value

    def number(name, default, minimum, maximum):
        try:
            value = int(environ.get(name, str(default)))
            if not minimum <= value <= maximum:
                raise ValueError
            return value
        except (TypeError, ValueError):
            raise ImproperlyConfigured(f'{name} is outside its permitted range.') from None

    def flag(name, default='0'):
        value = environ.get(name, default)
        if value not in ('0', '1'):
            raise ImproperlyConfigured(f'{name} must be 0 or 1.')
        return value == '1'

    if environ.get('ROUNDTABLE_ENV') != 'production':
        raise ImproperlyConfigured('Set ROUNDTABLE_ENV=production before loading production settings.')
    secret = required('DJANGO_SECRET_KEY')
    mfa = required('MFA_ENCRYPTION_KEY')
    if not valid_secret(secret) or not valid_fernet(mfa):
        raise ImproperlyConfigured('Production signing and encryption keys are missing or invalid.')
    hosts = [item.strip().lower() for item in required('DJANGO_ALLOWED_HOSTS').split(',')]
    if not hosts or not all(public_host(host) for host in hosts):
        raise ImproperlyConfigured('DJANGO_ALLOWED_HOSTS must list explicit public hosts without wildcards.')
    origin = required('PUBLIC_BASE_URL').rstrip('/')
    if not valid_public_origin(origin, hosts):
        raise ImproperlyConfigured('PUBLIC_BASE_URL must be an HTTPS origin included in DJANGO_ALLOWED_HOSTS.')
    if environ.get('PGSSLMODE', 'verify-full') != 'verify-full':
        raise ImproperlyConfigured('PGSSLMODE must be verify-full in this production configuration.')
    db_options = {'sslmode': 'verify-full', 'connect_timeout': 10}
    if environ.get('PGSSLROOTCERT'):
        db_options['sslrootcert'] = environ['PGSSLROOTCERT']
    database = {'ENGINE': 'django.db.backends.postgresql', 'NAME': required('POSTGRES_DB'),
                'USER': required('POSTGRES_USER'), 'PASSWORD': required('POSTGRES_PASSWORD'),
                'HOST': required('POSTGRES_HOST'), 'PORT': number('POSTGRES_PORT', 5432, 1, 65535),
                'CONN_MAX_AGE': 0, 'CONN_HEALTH_CHECKS': True, 'OPTIONS': db_options}
    email_host = required('EMAIL_HOST')
    email_user = required('EMAIL_HOST_USER')
    email_password = required('EMAIL_HOST_PASSWORD')
    sender = required('DEFAULT_FROM_EMAIL')
    address = parseaddr(sender)[1]
    if '\r' in sender or '\n' in sender or '@' not in address or not public_host(address.rsplit('@', 1)[1]):
        raise ImproperlyConfigured('DEFAULT_FROM_EMAIL must use a valid public sender domain.')
    use_ssl, use_tls = flag('EMAIL_USE_SSL'), flag('EMAIL_USE_TLS', '1')
    if use_ssl == use_tls:
        raise ImproperlyConfigured('Enable exactly one of EMAIL_USE_SSL and EMAIL_USE_TLS.')
    values = {
        'ENVIRONMENT': 'production', 'DEBUG': False, 'SECRET_KEY': secret, 'MFA_ENCRYPTION_KEY': mfa,
        'ALLOWED_HOSTS': hosts, 'PUBLIC_BASE_URL': origin, 'CSRF_TRUSTED_ORIGINS': [origin],
        'DATABASES': {'default': database}, 'SESSION_COOKIE_SECURE': True,
        'SESSION_COOKIE_HTTPONLY': True, 'CSRF_COOKIE_SECURE': True, 'SECURE_SSL_REDIRECT': True,
        'SECURE_HSTS_SECONDS': 31536000, 'SECURE_HSTS_INCLUDE_SUBDOMAINS': False,
        'SECURE_CONTENT_TYPE_NOSNIFF': True, 'X_FRAME_OPTIONS': 'DENY',
        'EMAIL_BACKEND': 'django.core.mail.backends.smtp.EmailBackend', 'EMAIL_DELIVERY_ENABLED': True,
        'EMAIL_HOST': email_host, 'EMAIL_PORT': number('EMAIL_PORT', 587, 1, 65535),
        'EMAIL_HOST_USER': email_user, 'EMAIL_HOST_PASSWORD': email_password,
        'EMAIL_USE_TLS': use_tls, 'EMAIL_USE_SSL': use_ssl, 'EMAIL_TIMEOUT': 15,
        'DEFAULT_FROM_EMAIL': sender, 'RELEASE_STAGE': 'internal',
        'PASSWORD_HASHERS': ['django.contrib.auth.hashers.PBKDF2PasswordHasher',
                            'django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher'],
    }
    # Enable only after the edge strips client-supplied forwarding headers and sets its own.
    values['SECURE_PROXY_SSL_HEADER'] = ('HTTP_X_FORWARDED_PROTO', 'https') if flag('ROUNDTABLE_TRUST_PROXY_TLS') else None
    return values
