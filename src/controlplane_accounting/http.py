"""Authenticated bounded HTTP adapter; no fallback and no upstream error bodies."""
import asyncio
from dataclasses import asdict
from urllib.parse import urlsplit
from .contract import (AccountingError, Scope, Mutation, Quantity, Quote, Reservation,
                       Usage, Balance, Entitlement, Settlement, Capabilities,
                       ERROR_STATUS, assert_scope, _text, _typed)
from .wire import decode


class HttpBackend:
    def __init__(self, url, *, token, authorize, units=('tokens','milliseconds'),
                 allow_insecure_local=False, transport=None, sleep=asyncio.sleep):
        import httpx
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except (ValueError, TypeError):
            raise AccountingError('invalid_request') from None
        local = (allow_insecure_local and parsed.scheme == 'http' and
                 parsed.hostname in ('localhost','127.0.0.1','::1'))
        if (not parsed.hostname or (parsed.scheme != 'https' and not local)
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or (port is not None and port < 1)):
            raise AccountingError('invalid_request')
        if not isinstance(token, str) or not token or any(c.isspace() or ord(c)<32 or ord(c)>126 for c in token):
            raise AccountingError('unauthenticated')
        if not callable(authorize):
            raise AccountingError('unauthenticated')
        Capabilities(Scope('validation','validation'), tuple(units), ())
        self.units, self.authorize, self.sleep = tuple(units), authorize, sleep
        self._client = httpx.AsyncClient(base_url=url.rstrip('/')+'/',
            headers={'Authorization':'Bearer '+token}, follow_redirects=False,
            timeout=httpx.Timeout(10.0, connect=2.0), transport=transport, trust_env=False)

    async def aclose(self):
        await self._client.aclose()

    async def _call(self, operation, scope, cls, mutation=None, **payload):
        import httpx
        _typed(scope, Scope)
        if self.authorize(scope, operation) is not True:
            raise AccountingError('forbidden')
        for value in payload.values():
            if isinstance(value, Quantity):
                if value.unit not in self.units:
                    raise AccountingError('invalid_request')
            else:
                _text(value)
        if 'unit' in payload and payload['unit'] not in self.units:
            raise AccountingError('invalid_request')
        body = {'scope':asdict(scope), **{k:asdict(v) if isinstance(v, Quantity) else v for k,v in payload.items()}}
        headers = {}
        if mutation is not None:
            _typed(mutation, Mutation)
            assert_scope(scope, mutation.scope)
            headers['Idempotency-Key'] = mutation.idempotency_key
        try:
            async with asyncio.timeout(30):
                for attempt in range(3):
                    delay = 0.1 * 2**attempt
                    try:
                        async with asyncio.timeout(10):
                            response = await self._client.post('api/v1/accounting/'+operation, json=body, headers=headers)
                    except (httpx.TransportError, TimeoutError):
                        if attempt == 2:
                            raise AccountingError('unavailable') from None
                    else:
                        if response.status_code == 200:
                            try:
                                result = decode(cls, response.json())
                                self._validate(result, scope, payload, operation)
                                return result
                            except (ValueError, TypeError, KeyError, AttributeError):
                                raise AccountingError('unavailable') from None
                        if response.status_code not in (429,502,503,504):
                            try:
                                code = response.json()['code']
                                if code not in ERROR_STATUS or ERROR_STATUS[code] != response.status_code:
                                    code = 'unavailable'
                            except (ValueError, TypeError, KeyError):
                                code = 'unavailable'
                            raise AccountingError(code)
                        try:
                            retry_after = float(response.headers.get('Retry-After', '0'))
                            if not 0 <= retry_after <= 5:
                                raise AccountingError('unavailable')
                            delay = max(delay, retry_after)
                        except ValueError:
                            raise AccountingError('unavailable') from None
                        if attempt == 2:
                            raise AccountingError('unavailable')
                    await self.sleep(delay)
        except TimeoutError:
            raise AccountingError('unavailable') from None
        raise AccountingError('unavailable')

    def _validate(self, result, scope, payload, operation):
        assert_scope(scope, result.scope)
        if isinstance(result, Capabilities) and not set(self.units) <= set(result.units):
            raise AccountingError('invalid_request')
        for name in ('resource','quote_id','reservation_id','meter','period'):
            if name in payload and getattr(result, name, None) != payload[name]:
                raise AccountingError('invalid_request')
        if 'quantity' in payload:
            expected = payload['quantity']
            actual = result.quantity
            if actual.unit != expected.unit:
                raise AccountingError('invalid_request')
            if isinstance(result, (Quote, Usage)) or 'quote_id' in payload:
                if actual != expected:
                    raise AccountingError('invalid_request')
            elif expected.amount > actual.amount:
                raise AccountingError('invalid_request')
            if operation in ('commit','release'):
                field = 'committed' if operation == 'commit' else 'released'
                if getattr(result, field).amount < expected.amount:
                    raise AccountingError('invalid_request')
        if isinstance(result, Balance) and result.available.unit != payload['unit']:
            raise AccountingError('invalid_request')

    async def capabilities(self, scope):
        return await self._call('capabilities',scope,Capabilities)
    async def quote(self, mutation, resource, quantity):
        return await self._call('quote',mutation.scope,Quote,mutation,resource=resource,quantity=quantity)
    async def reserve(self, mutation, quote_id, quantity):
        return await self._call('reserve',mutation.scope,Reservation,mutation,quote_id=quote_id,quantity=quantity)
    async def commit(self, mutation, reservation_id, quantity):
        return await self._call('commit',mutation.scope,Reservation,mutation,reservation_id=reservation_id,quantity=quantity)
    async def release(self, mutation, reservation_id, quantity):
        return await self._call('release',mutation.scope,Reservation,mutation,reservation_id=reservation_id,quantity=quantity)
    async def record_usage(self, mutation, meter, resource, quantity):
        return await self._call('record_usage',mutation.scope,Usage,mutation,meter=meter,resource=resource,quantity=quantity)
    async def balance(self, scope, unit):
        return await self._call('balance',scope,Balance,unit=unit)
    async def entitlement(self, scope, resource, *, reference=None):
        if reference is not None:
            if not (await self.capabilities(scope)).reference_entitlements:
                raise AccountingError('unsupported')
            result = await self._call('entitlement',scope,Entitlement,resource=resource,reference=reference)
            if result.allowed and result.entitlement_id != reference:
                raise AccountingError('invalid_request')
            return result
        return await self._call('entitlement',scope,Entitlement,resource=resource)
    async def settle(self, mutation, period):
        return await self._call('settle',mutation.scope,Settlement,mutation,period=period)
