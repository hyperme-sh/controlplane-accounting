"""Explicit backend selection from trusted operator configuration."""
from .contract import AccountingError
from .backends import InternalBackend, NoneBackend
from .repository import PostgresRepository


def configured_backend(config, *, authorize, units=('tokens','milliseconds')):
    mode = config.get('ACCOUNTING_BACKEND', 'internal')
    if mode == 'http':
        from .http import HttpBackend
        url = config.get('ACCOUNTING_URL')
        if not url:
            raise AccountingError('invalid_request')
        return HttpBackend(url, token=config.get('ACCOUNTING_TOKEN'), authorize=authorize, units=units)
    if mode not in ('internal','none'):
        raise AccountingError('invalid_request')
    dsn = config.get('ACCOUNTING_DATABASE_URL')
    if not dsn:
        raise AccountingError('invalid_request')
    repository = PostgresRepository(dsn)
    backend = InternalBackend if mode == 'internal' else NoneBackend
    raw=config.get('ACCOUNTING_RESERVATION_TTL_SECONDS','3600')
    if not isinstance(raw,str) or not raw.isascii() or not raw.isdecimal():
        raise AccountingError('invalid_request')
    return backend(repository, authorize=authorize, units=units, reservation_ttl=int(raw))
