"""Integer quantities and scoped ports shared by independently updated peers."""
from dataclasses import asdict, dataclass
import json
import re
from typing import Protocol, runtime_checkable

MAX_AMOUNT = 2**63 - 1
OPERATIONS = frozenset({'capabilities', 'quote', 'reserve', 'commit', 'release',
                        'record_usage', 'balance', 'entitlement', 'settle'})
ERROR_STATUS = {'invalid_request': 400, 'unauthenticated': 401, 'forbidden': 403,
                'not_found': 404, 'conflict': 409, 'expired': 410,
                'quota_exceeded': 422, 'unsupported': 501, 'unavailable': 503}


class AccountingError(ValueError):
    """Stable safe code only; never expose upstream bodies or credentials."""
    def __init__(self, code: str):
        if code not in ERROR_STATUS:
            raise ValueError('unknown accounting error')
        self.code = code
        self.status_code = ERROR_STATUS[code]
        super().__init__(code)


def _text(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}', value):
        raise AccountingError('invalid_request')


def _integer(value):
    if type(value) is not int or not 0 <= value <= MAX_AMOUNT:
        raise AccountingError('invalid_request')


@dataclass(frozen=True)
class Scope:
    tenant_id: str
    subject_id: str

    def __post_init__(self):
        _text(self.tenant_id)
        _text(self.subject_id)


@dataclass(frozen=True)
class Quantity:
    amount: int
    unit: str

    def __post_init__(self):
        _integer(self.amount)
        _text(self.unit)


def _typed(value, cls):
    if not isinstance(value, cls):
        raise AccountingError('invalid_request')


@dataclass(frozen=True)
class Mutation:
    scope: Scope
    idempotency_key: str

    def __post_init__(self):
        _typed(self.scope, Scope)
        _text(self.idempotency_key)


@dataclass(frozen=True)
class Capabilities:
    scope: Scope
    units: tuple[str, ...]
    operations: tuple[str, ...]
    checkout: bool = False
    reference_entitlements: bool = False

    def __post_init__(self):
        _typed(self.scope, Scope)
        if type(self.units) is not tuple or not self.units or any(not isinstance(x, str) for x in self.units) or len(set(self.units)) != len(self.units):
            raise AccountingError('invalid_request')
        for unit in self.units:
            _text(unit)
        if type(self.operations) is not tuple or any(not isinstance(x, str) for x in self.operations) or not set(self.operations) <= OPERATIONS or type(self.checkout) is not bool or type(self.reference_entitlements) is not bool:
            raise AccountingError('invalid_request')


@dataclass(frozen=True)
class Quote:
    scope: Scope
    quote_id: str
    resource: str
    quantity: Quantity
    expires_at: int

    def __post_init__(self):
        _typed(self.scope, Scope)
        _text(self.quote_id)
        _text(self.resource)
        _typed(self.quantity, Quantity)
        _integer(self.expires_at)


@dataclass(frozen=True)
class Reservation:
    scope: Scope
    reservation_id: str
    quote_id: str
    quantity: Quantity
    committed: Quantity
    released: Quantity
    expires_at: int

    def __post_init__(self):
        _typed(self.scope, Scope)
        _text(self.reservation_id)
        _text(self.quote_id)
        _integer(self.expires_at)
        for value in (self.quantity, self.committed, self.released):
            _typed(value, Quantity)
        for value in (self.quantity, self.committed, self.released):
            if value.unit != self.quantity.unit:
                raise AccountingError('invalid_request')
        if self.committed.amount + self.released.amount > self.quantity.amount:
            raise AccountingError('conflict')


@dataclass(frozen=True)
class Usage:
    scope: Scope
    usage_id: str
    meter: str
    resource: str
    quantity: Quantity

    def __post_init__(self):
        _typed(self.scope, Scope)
        for value in (self.usage_id, self.meter, self.resource):
            _text(value)
        _typed(self.quantity, Quantity)


@dataclass(frozen=True)
class Balance:
    scope: Scope
    available: Quantity
    reserved: Quantity
    consumed: Quantity

    def __post_init__(self):
        _typed(self.scope, Scope)
        for value in (self.available, self.reserved, self.consumed):
            _typed(value, Quantity)
        for value in (self.available, self.reserved, self.consumed):
            if value.unit != self.available.unit:
                raise AccountingError('invalid_request')


@dataclass(frozen=True)
class Entitlement:
    scope: Scope
    entitlement_id: str
    resource: str
    allowed: bool
    expires_at: int
    quantity: Quantity | None = None

    def __post_init__(self):
        if self.quantity is not None:
            _typed(self.quantity, Quantity)
        _typed(self.scope, Scope)
        _text(self.entitlement_id)
        _text(self.resource)
        _integer(self.expires_at)
        if type(self.allowed) is not bool:
            raise AccountingError('invalid_request')


@dataclass(frozen=True)
class Settlement:
    scope: Scope
    settlement_id: str
    period: str
    status: str

    def __post_init__(self):
        _typed(self.scope, Scope)
        _text(self.settlement_id)
        _text(self.period)
        if self.status not in ('accepted', 'pending', 'refused'):
            raise AccountingError('invalid_request')


def assert_scope(expected: Scope, actual: Scope):
    _typed(expected, Scope)
    _typed(actual, Scope)
    if expected != actual:
        raise AccountingError('not_found')


def validate_quote(quote: Quote, scope: Scope, quantity: Quantity, now: int):
    _typed(quote, Quote)
    _typed(quantity, Quantity)
    _integer(now)
    assert_scope(scope, quote.scope)
    if now >= quote.expires_at:
        raise AccountingError('expired')
    if quantity != quote.quantity:
        raise AccountingError('conflict')


def canonical_request(operation: str, mutation: Mutation, **payload) -> bytes:
    """Bind an operation and its known typed payload to a scoped replay key.

    Storage must atomically compare these bytes and persist the original response
    with the mutation. This helper is not a durable replay store.
    """
    _typed(mutation, Mutation)
    if operation not in OPERATIONS - {'capabilities', 'balance', 'entitlement'}:
        raise AccountingError('invalid_request')
    def encode(value):
        if isinstance(value, (Scope, Quantity, Quote)):
            return asdict(value)
        raise TypeError('unsupported canonical payload')
    return json.dumps({'operation': operation, 'mutation': asdict(mutation), 'payload': payload},
                      default=encode, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')


@runtime_checkable
class Accounting(Protocol):
    async def capabilities(self, scope: Scope) -> Capabilities: ...
    async def quote(self, mutation: Mutation, resource: str, quantity: Quantity) -> Quote: ...
    async def reserve(self, mutation: Mutation, quote_id: str, quantity: Quantity) -> Reservation: ...
    async def commit(self, mutation: Mutation, reservation_id: str, quantity: Quantity) -> Reservation: ...
    async def release(self, mutation: Mutation, reservation_id: str, quantity: Quantity) -> Reservation: ...
    async def record_usage(self, mutation: Mutation, meter: str, resource: str, quantity: Quantity) -> Usage: ...
    async def balance(self, scope: Scope, unit: str) -> Balance: ...
    async def entitlement(self, scope: Scope, resource: str, *, reference: str | None = None) -> Entitlement: ...
    async def settle(self, mutation: Mutation, period: str) -> Settlement: ...


def replay_result(original_request: bytes, original_result, request: bytes):
    """Resolve a stored replay inside the backend's transaction and auth scope."""
    if type(original_request) is not bytes or type(request) is not bytes:
        raise AccountingError('invalid_request')
    if original_request != request:
        raise AccountingError('conflict')
    return original_result


def organization_scope(tenant_id: str, organization_id: str) -> Scope:
    """Canonical organization principal; membership must be checked by the owner.

    Namespacing keeps organization balances separate from historical actor rows.
    Existing actor scopes and their outstanding references remain unchanged.
    """
    import uuid
    try:
        tenant, organization = uuid.UUID(tenant_id), uuid.UUID(organization_id)
    except (ValueError, TypeError, AttributeError):
        raise AccountingError('invalid_request') from None
    if str(tenant) != tenant_id or str(organization) != organization_id:
        raise AccountingError('invalid_request')
    return Scope(tenant_id, 'org:' + organization_id)
