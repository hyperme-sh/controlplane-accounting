"""v1 response decoding: require known fields, tolerate additive object fields."""
from dataclasses import fields
from .contract import (AccountingError, Scope, Quantity, Quote, Reservation,
                       Usage, Balance, Entitlement, Settlement, Capabilities)

_TYPES = (Scope, Quantity, Quote, Reservation, Usage, Balance, Entitlement, Settlement, Capabilities)
_QUANTITIES = {'quantity', 'committed', 'released', 'available', 'reserved', 'consumed'}


def decode(cls, body):
    if cls not in _TYPES or not isinstance(body, dict):
        raise AccountingError('invalid_request')
    values = {}
    for field in fields(cls):
        if field.name not in body:
            # checkout was optional in the initial v1 response.
            if (cls is Capabilities and field.name in ('checkout', 'reference_entitlements')) or (cls is Entitlement and field.name == 'quantity'):
                continue
            raise AccountingError('invalid_request')
        value = body[field.name]
        if field.name == 'scope':
            value = decode(Scope, value)
        elif field.name in _QUANTITIES:
            if not (cls is Entitlement and field.name == 'quantity' and value is None):
                value = decode(Quantity, value)
        elif cls is Capabilities and field.name in ('units', 'operations'):
            if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
                raise AccountingError('invalid_request')
            value = tuple(value)
        values[field.name] = value
    return cls(**values)
