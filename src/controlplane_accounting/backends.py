"""Internal quotas and explicit unlimited lab mode share scoped durable storage."""
from dataclasses import asdict, replace
import time
import uuid
from .contract import (
    AccountingError, Scope, Mutation, Quantity, Quote, Reservation, Usage,
    Balance, Entitlement, Capabilities, MAX_AMOUNT, canonical_request,
    validate_quote, _text, _typed, _integer,
)
from .wire import decode


def _id():
    return uuid.uuid4().hex


class InternalBackend:
    """authorize(scope, operation) must raise on denial; supplied by trusted owner.

    No request-supplied flags grant administrative authority. Repository commits
    state and original responses together or rolls everything back.
    """
    unlimited = False

    def __init__(self, repository, *, authorize, units=('tokens', 'milliseconds'),
                 clock=time.time, quote_ttl=300, reservation_ttl=3600):
        if not callable(authorize):
            raise AccountingError('unauthenticated')
        Capabilities(Scope('validation', 'validation'), tuple(units), ())
        for ttl, maximum in ((quote_ttl,86400),(reservation_ttl,32*86400)):
            if type(ttl) is not int or not 1 <= ttl <= maximum:
                raise AccountingError('invalid_request')
        self.repository, self.authorize = repository, authorize
        self.units, self.clock = tuple(units), clock
        self.quote_ttl, self.reservation_ttl = quote_ttl, reservation_ttl

    def _auth(self, scope, operation):
        _typed(scope, Scope)
        # A false return is also a denial; successful policy callbacks return True.
        if self.authorize(scope, operation) is not True:
            raise AccountingError('forbidden')

    def _quantity(self, quantity):
        _typed(quantity, Quantity)
        if quantity.unit not in self.units:
            raise AccountingError('invalid_request')

    def _now(self):
        now = int(self.clock())
        _integer(now)
        return now

    def _state(self, state, now):
        for key in ('grants', 'quotes', 'reservations', 'usage', 'replays', 'quote_reservations'):
            state.setdefault(key, {})
        # Retain expired references and replay rows; only unused quota is freed.
        for rid, raw in state['reservations'].items():
            reservation = decode(Reservation, raw)
            if reservation.expires_at <= now:
                unused = reservation.quantity.amount - reservation.committed.amount
                state['reservations'][rid] = asdict(replace(reservation, released=Quantity(unused, reservation.quantity.unit)))
        return state

    def _totals(self, state, unit):
        reserved = consumed = 0
        for raw in state['reservations'].values():
            r = decode(Reservation, raw)
            if r.quantity.unit == unit:
                reserved += r.quantity.amount - r.committed.amount - r.released.amount
                consumed += r.committed.amount
        if reserved + consumed > MAX_AMOUNT:
            raise AccountingError('quota_exceeded')
        return reserved, consumed

    async def _mutate(self, operation, mutation, cls, apply, **payload):
        _typed(mutation, Mutation)
        self._auth(mutation.scope, operation)
        if operation == 'grant':
            # Administrative extension has the same cross-operation replay space.
            import json
            request = json.dumps({'operation':'grant', 'mutation':asdict(mutation),
                                  'quantity':asdict(payload['quantity'])}, sort_keys=True)
        else:
            request = canonical_request(operation, mutation, **payload).decode()
        async with self.repository.transaction(mutation.scope) as raw:
            now = self._now()
            state = self._state(raw, now)
            previous = state['replays'].get(mutation.idempotency_key)
            if previous:
                if previous['request'] != request:
                    raise AccountingError('conflict')
                return decode(cls, previous['result'])
            result = apply(state, now)
            state['replays'][mutation.idempotency_key] = {'request':request, 'result':asdict(result)}
            return result

    async def grant(self, mutation, quantity):
        """Add an explicit quota grant. Only a trusted grant policy may allow this."""
        self._quantity(quantity)
        def apply(state, now):
            total = state['grants'].get(quantity.unit, 0) + quantity.amount
            if total > MAX_AMOUNT:
                raise AccountingError('invalid_request')
            state['grants'][quantity.unit] = total
            return self._balance(state, mutation.scope, quantity.unit)
        return await self._mutate('grant', mutation, Balance, apply, quantity=quantity)

    async def capabilities(self, scope):
        self._auth(scope, 'capabilities')
        return Capabilities(scope, self.units, ('capabilities','quote','reserve','commit','release','record_usage','balance','entitlement'), reference_entitlements=True)

    async def quote(self, mutation, resource, quantity):
        _text(resource)
        self._quantity(quantity)
        def apply(state, now):
            quote = Quote(mutation.scope, _id(), resource, quantity, now + self.quote_ttl)
            state['quotes'][quote.quote_id] = asdict(quote)
            return quote
        return await self._mutate('quote', mutation, Quote, apply, resource=resource, quantity=quantity)

    async def reserve(self, mutation, quote_id, quantity):
        _text(quote_id)
        self._quantity(quantity)
        def apply(state, now):
            raw = state['quotes'].get(quote_id)
            if raw is None:
                raise AccountingError('not_found')
            quote = decode(Quote, raw)
            validate_quote(quote, mutation.scope, quantity, now)
            if quote_id in state['quote_reservations']:
                raise AccountingError('conflict')
            balance = self._balance(state, mutation.scope, quantity.unit)
            if quantity.amount > balance.available.amount:
                raise AccountingError('quota_exceeded')
            r = Reservation(mutation.scope, _id(), quote_id, quantity,
                            Quantity(0, quantity.unit), Quantity(0, quantity.unit), now + self.reservation_ttl)
            state['reservations'][r.reservation_id] = asdict(r)
            state['quote_reservations'][quote_id] = r.reservation_id
            return r
        return await self._mutate('reserve', mutation, Reservation, apply, quote_id=quote_id, quantity=quantity)

    async def _change(self, operation, mutation, reservation_id, quantity):
        _text(reservation_id)
        self._quantity(quantity)
        def apply(state, now):
            raw = state['reservations'].get(reservation_id)
            if raw is None:
                raise AccountingError('not_found')
            r = decode(Reservation, raw)
            if now >= r.expires_at:
                raise AccountingError('expired')
            if quantity.unit != r.quantity.unit:
                raise AccountingError('conflict')
            remaining = r.quantity.amount - r.committed.amount - r.released.amount
            if quantity.amount > remaining:
                raise AccountingError('conflict')
            field = 'committed' if operation == 'commit' else 'released'
            r = replace(r, **{field:Quantity(getattr(r, field).amount + quantity.amount, quantity.unit)})
            state['reservations'][reservation_id] = asdict(r)
            return r
        return await self._mutate(operation, mutation, Reservation, apply, reservation_id=reservation_id, quantity=quantity)

    async def commit(self, mutation, reservation_id, quantity):
        return await self._change('commit', mutation, reservation_id, quantity)

    async def release(self, mutation, reservation_id, quantity):
        return await self._change('release', mutation, reservation_id, quantity)

    async def record_usage(self, mutation, meter, resource, quantity):
        _text(meter)
        _text(resource)
        self._quantity(quantity)
        def apply(state, now):
            usage = Usage(mutation.scope, _id(), meter, resource, quantity)
            state['usage'][usage.usage_id] = asdict(usage)
            return usage
        return await self._mutate('record_usage', mutation, Usage, apply, meter=meter, resource=resource, quantity=quantity)

    def _balance(self, state, scope, unit):
        reserved, consumed = self._totals(state, unit)
        limit = MAX_AMOUNT if self.unlimited else state['grants'].get(unit, 0)
        return Balance(scope, Quantity(limit - reserved - consumed, unit), Quantity(reserved, unit), Quantity(consumed, unit))

    async def balance(self, scope, unit):
        self._auth(scope, 'balance')
        self._quantity(Quantity(0, unit))
        async with self.repository.transaction(scope) as state:
            return self._balance(self._state(state, self._now()), scope, unit)

    async def usage(self, scope):
        self._auth(scope, 'usage')
        async with self.repository.transaction(scope) as state:
            return tuple(decode(Usage, u) for u in self._state(state, self._now())['usage'].values())

    async def entitlement(self, scope, resource, *, reference=None):
        self._auth(scope, 'entitlement')
        _text(resource)
        if reference is not None: _text(reference)
        async with self.repository.transaction(scope) as raw:
            now = self._now()
            state = self._state(raw, now)
            candidates = [decode(Reservation, r) for r in state['reservations'].values()
                          if state['quotes'][r['quote_id']]['resource'] == resource
                          and (reference is None or r['reservation_id'] == reference)]
            valid = [r for r in candidates if r.expires_at > now and r.quantity.amount > r.released.amount]
            if valid:
                r = max(valid, key=lambda r:(r.expires_at, r.reservation_id))
                return Entitlement(scope, r.reservation_id, resource, True, r.expires_at,
                    Quantity(r.quantity.amount-r.released.amount, r.quantity.unit))
            return Entitlement(scope, 'none', resource, False, now)

    async def settle(self, mutation, period):
        _typed(mutation, Mutation)
        self._auth(mutation.scope, 'settle')
        _text(period)
        raise AccountingError('unsupported')


class NoneBackend(InternalBackend):
    """Explicit lab selection bypasses quota limits, never scope or durable usage."""
    unlimited = True
