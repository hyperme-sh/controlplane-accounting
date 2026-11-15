"""Organization quota projection with actor-private replay identities.

The serving owner must verify fresh membership, retain actor audit records and
persist the chosen quota scope alongside its intent before calling this adapter.
An existing intent must never be moved between quota scopes. The owner must
check actor-private reference ownership before commit/release: the underlying
quota principal is shared by every authorized organization member.
"""
from dataclasses import replace
import hashlib
from .contract import AccountingError, Scope, Mutation, assert_scope, organization_scope


class OrganizationBackend:
    def __init__(self, backend, *, actor: Scope, organization_id: str, entitlement_reference=None):
        if not isinstance(actor, Scope):
            raise AccountingError('invalid_request')
        self.entitlement_reference = entitlement_reference
        self.backend = backend
        self.actor = actor
        self.quota_scope = organization_scope(actor.tenant_id, organization_id)

    def _scope(self, actor):
        assert_scope(self.actor, actor)
        return self.quota_scope

    def _mutation(self, mutation):
        self._scope(mutation.scope)
        # Stable bounded names isolate actors even when caller keys coincide.
        key = hashlib.sha256((self.actor.subject_id+'\0'+mutation.idempotency_key).encode()).hexdigest()
        return Mutation(self.quota_scope, 'actor.' + key)

    def _result(self, result):
        assert_scope(self.quota_scope, result.scope)
        return replace(result, scope=self.actor)

    async def capabilities(self,scope):
        return self._result(await self.backend.capabilities(self._scope(scope)))
    async def balance(self,scope,unit):
        return self._result(await self.backend.balance(self._scope(scope),unit))
    async def entitlement(self,scope,resource):
        quota_scope = self._scope(scope)
        if self.entitlement_reference is None:
            raise AccountingError('unsupported')
        reference = await self.entitlement_reference(scope, resource)
        if not reference:
            raise AccountingError('forbidden')
        result = await self.backend.entitlement(quota_scope, resource, reference=reference)
        if result.resource != resource or (result.allowed and result.entitlement_id != reference):
            raise AccountingError('forbidden')
        return self._result(result)
    async def quote(self,mutation,resource,quantity):
        return self._result(await self.backend.quote(self._mutation(mutation),resource,quantity))
    async def reserve(self,mutation,quote_id,quantity):
        return self._result(await self.backend.reserve(self._mutation(mutation),quote_id,quantity))
    async def commit(self,mutation,reservation_id,quantity):
        return self._result(await self.backend.commit(self._mutation(mutation),reservation_id,quantity))
    async def release(self,mutation,reservation_id,quantity):
        return self._result(await self.backend.release(self._mutation(mutation),reservation_id,quantity))
    async def record_usage(self,mutation,meter,resource,quantity):
        return self._result(await self.backend.record_usage(self._mutation(mutation),meter,resource,quantity))
    async def settle(self,mutation,period):
        return self._result(await self.backend.settle(self._mutation(mutation),period))
