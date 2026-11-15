import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import asdict
import json
import pytest
import httpx
from controlplane_accounting import Scope, Mutation, Quantity, AccountingError
from controlplane_accounting.backends import InternalBackend, NoneBackend
from controlplane_accounting.http import HttpBackend
from controlplane_accounting.config import configured_backend

pytestmark = pytest.mark.asyncio
S = Scope('tenant-a','subject-a')
OTHER = Scope('tenant-b','subject-b')
Q = lambda n: Quantity(n,'tokens')
M = lambda key: Mutation(S,key)


class RepositoryDouble:
    """Transaction contract double: rollback on errors, shared durable snapshots."""
    def __init__(self):
        self.rows = {}
        self.lock = asyncio.Lock()
    @asynccontextmanager
    async def transaction(self, scope):
        async with self.lock:
            state = deepcopy(self.rows.get(scope, {}))
            yield state
            self.rows[scope] = deepcopy(state)


def backend(cls=InternalBackend, **kwargs):
    return cls(RepositoryDouble(), authorize=lambda s,o: s == S, **kwargs)


async def funded(b, amount=10):
    await b.grant(M('grant'),Q(amount))
    q = await b.quote(M('quote'),'resource-a',Q(amount))
    return await b.reserve(M('reserve'),q.quote_id,Q(amount))


@pytest.mark.parametrize('cls',[InternalBackend,NoneBackend])
async def test_complete_slice_replay_and_resource_scope(cls):
    b = backend(cls)
    r = await funded(b)
    committed = await b.commit(M('commit'),r.reservation_id,Q(4))
    assert await b.commit(M('commit'),r.reservation_id,Q(4)) == committed
    released = await b.release(M('release'),r.reservation_id,Q(6))
    assert await b.release(M('release'),r.reservation_id,Q(6)) == released
    usage = await b.record_usage(M('usage'),'inference','resource-a',Q(4))
    assert await b.record_usage(M('usage'),'inference','resource-a',Q(4)) == usage
    assert len(await b.usage(S)) == 1
    balance = await b.balance(S,'tokens')
    assert (balance.reserved.amount,balance.consumed.amount) == (0,4)
    assert (await b.entitlement(S,'resource-a')).allowed
    assert not (await b.entitlement(S,'unrelated')).allowed
    for operation in (
        lambda: b.commit(M('commit'),r.reservation_id,Q(5)),
        lambda: b.release(M('commit'),r.reservation_id,Q(4)),
        lambda: b.commit(M('over'),r.reservation_id,Q(7)),
    ):
        with pytest.raises(AccountingError,match='conflict'):
            await operation()
    with pytest.raises(AccountingError,match='forbidden'):
        await b.balance(OTHER,'tokens')
    with pytest.raises(AccountingError,match='unsupported'):
        await b.settle(M('settle'),'period-a')


async def test_competing_reservations_and_quote_single_use():
    b = backend()
    await b.grant(M('grant'),Q(10))
    quotes = [await b.quote(M(f'q{i}'),'resource-a',Q(7)) for i in range(2)]
    results = await asyncio.gather(*(b.reserve(M(f'r{i}'),q.quote_id,Q(7)) for i,q in enumerate(quotes)), return_exceptions=True)
    assert sum(isinstance(r,AccountingError) for r in results) == 1
    assert (await b.balance(S,'tokens')).available.amount == 3
    r = next(r for r in results if not isinstance(r,Exception))
    with pytest.raises(AccountingError,match='conflict'):
        await b.reserve(M('again'),r.quote_id,Q(7))


async def test_scope_reference_and_grant_authority():
    b = backend()
    r = await funded(b)
    b.authorize = lambda s,o: o != 'grant'
    with pytest.raises(AccountingError,match='forbidden'):
        await b.grant(M('grant'),Q(10))  # replays authenticate too
    with pytest.raises(AccountingError,match='not_found'):
        await b.commit(Mutation(OTHER,'cross'),r.reservation_id,Q(1))
    assert (await b.balance(OTHER,'tokens')).available.amount == 0


async def test_restart_expiry_cleanup_and_replay_retention():
    now = [100]
    b = backend(clock=lambda:now[0],reservation_ttl=10)
    r = await funded(b)
    await b.commit(M('commit'),r.reservation_id,Q(3))
    now[0] = 111
    restarted = InternalBackend(b.repository,authorize=b.authorize,clock=lambda:now[0])
    bal = await restarted.balance(S,'tokens')
    assert (bal.available.amount,bal.reserved.amount,bal.consumed.amount) == (7,0,3)
    assert await restarted.reserve(M('reserve'),r.quote_id,Q(10)) == r
    assert not (await restarted.entitlement(S,'resource-a')).allowed
    with pytest.raises(AccountingError,match='expired'):
        await restarted.commit(M('late'),r.reservation_id,Q(1))


async def test_none_requires_explicit_selection_and_still_authorizes():
    b = backend(NoneBackend)
    q = await b.quote(M('q'),'resource-a',Q(50))
    await b.reserve(M('r'),q.quote_id,Q(50))
    with pytest.raises(AccountingError,match='forbidden'):
        await b.record_usage(Mutation(OTHER,'x'),'meter','resource',Q(1))
    for config in ({},{'ACCOUNTING_BACKEND':'bad'},{'ACCOUNTING_BACKEND':'http'},
                   {'ACCOUNTING_BACKEND':'none'}):
        with pytest.raises(AccountingError):
            configured_backend(config,authorize=lambda s,o:True)
    selected = configured_backend({'ACCOUNTING_DATABASE_URL':'postgresql://local/accounting'},authorize=lambda s,o:True)
    assert type(selected) is InternalBackend


async def test_http_lost_response_replays_exact_payload_once():
    b = backend()
    calls = []
    async def handler(request):
        calls.append(request)
        data = json.loads(request.content)
        scope = Scope(**data['scope'])
        result = await b.record_usage(Mutation(scope,request.headers['Idempotency-Key']),data['meter'],data['resource'],Quantity(**data['quantity']))
        if len(calls) == 1:
            raise httpx.ReadTimeout('lost response')
        return httpx.Response(200,json=asdict(result))
    h = HttpBackend('https://accounting.example',token='secret',authorize=lambda s,o:s==S,transport=httpx.MockTransport(handler),sleep=lambda _:asyncio.sleep(0))
    try:
        await h.record_usage(M('usage'),'meter','resource',Q(3))
        assert len(calls) == 2
        assert calls[0].content == calls[1].content
        assert calls[0].headers['authorization'] == 'Bearer secret'
        assert len(await b.usage(S)) == 1
    finally:
        await h.aclose()


@pytest.mark.parametrize('url',['http://remote.example','https://user:pass@example.com',
                               'https://example.com?token=x','file:///tmp/x','', 'https://example.com:bad'])
async def test_http_invalid_endpoint(url):
    with pytest.raises(AccountingError):
        HttpBackend(url,token='secret',authorize=lambda s,o:True)


@pytest.mark.parametrize('status,body',[(200,{'scope':asdict(OTHER),'available':asdict(Q(1)),'reserved':asdict(Q(0)),'consumed':asdict(Q(0))}),
    (200,{'scope':asdict(S),'available':{'amount':1,'unit':'hours'},'reserved':{'amount':0,'unit':'hours'},'consumed':{'amount':0,'unit':'hours'}}),
    (302,{}),(401,{'code':'unauthenticated'}),(409,{'code':'conflict'}),(503,{'code':'unavailable'})])
async def test_http_fails_closed_for_scope_units_and_errors(status,body):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(status,json=body)
    h=HttpBackend('https://accounting.example',token='secret',authorize=lambda s,o:True,transport=httpx.MockTransport(handler),sleep=lambda _:asyncio.sleep(0))
    try:
        with pytest.raises(AccountingError):
            await h.balance(S,'tokens')
        assert len(calls) == (3 if status == 503 else 1)
    finally:
        await h.aclose()


async def test_long_reservation_keeps_original_expiry_on_replay():
    now=[1000]
    b=backend(clock=lambda:now[0],reservation_ttl=32*86400)
    await b.grant(M('grant-long'),Quantity(100,'tokens'))
    q=await b.quote(M('quote-long'),'lease:long',Quantity(100,'tokens'))
    r=await b.reserve(M('reserve-long'),q.quote_id,q.quantity)
    now[0]+=30*86400
    assert await b.reserve(M('reserve-long'),q.quote_id,q.quantity)==r
    result=await b.commit(M('commit-long'),r.reservation_id,Quantity(40,'tokens'))
    assert result.committed.amount==40
    now[0]=r.expires_at
    with pytest.raises(AccountingError,match='expired'):
        await b.commit(M('expired-long'),r.reservation_id,Quantity(1,'tokens'))


@pytest.mark.parametrize('value',['0','2764801','-1','true','1.5',True,86400])
def test_config_refuses_unbounded_or_untyped_reservation_windows(value):
    with pytest.raises(AccountingError):
        configured_backend({'ACCOUNTING_DATABASE_URL':'postgresql://local/accounting',
            'ACCOUNTING_RESERVATION_TTL_SECONDS':value},authorize=lambda s,o:True)


def test_config_accepts_explicit_long_reservation_window():
    selected=configured_backend({'ACCOUNTING_DATABASE_URL':'postgresql://local/accounting',
        'ACCOUNTING_RESERVATION_TTL_SECONDS':str(32*86400)},authorize=lambda s,o:True)
    assert selected.reservation_ttl==32*86400


async def test_http_capabilities_accept_remote_superset_but_require_local_units():
    from controlplane_accounting import Capabilities
    backend = HttpBackend('https://accounting.invalid', token='unit-only', authorize=lambda s,o: True, units=('milliseconds',))
    backend._validate(Capabilities(S, ('tokens','milliseconds'), ('quote',)), S, {}, 'capabilities')
    with pytest.raises(AccountingError):
        backend._validate(Capabilities(S, ('tokens',), ('quote',)), S, {}, 'capabilities')
    await backend.aclose()


async def test_entitlement_quantity_tracks_unit_and_release():
    b=backend()
    r=await funded(b,10)
    assert (await b.entitlement(S,'resource-a')).quantity == Q(10)
    await b.release(M('release'),r.reservation_id,Q(6))
    assert (await b.entitlement(S,'resource-a')).quantity == Q(4)


async def test_organization_quota_is_shared_without_aliasing_personal_balance():
    from controlplane_accounting import organization_scope
    org=organization_scope('00000000-0000-4000-8000-000000000001','00000000-0000-4000-8000-000000000002')
    b=InternalBackend(RepositoryDouble(),authorize=lambda s,o:True)
    await b.grant(Mutation(org,'grant'),Q(10))
    for actor in ('first','second'):
        q=await b.quote(Mutation(org,actor+'.quote'),'resource-'+actor,Q(4))
        r=await b.reserve(Mutation(org,actor+'.reserve'),q.quote_id,Q(4))
        await b.commit(Mutation(org,actor+'.commit'),r.reservation_id,Q(4))
    assert (await b.balance(org,'tokens')).consumed.amount==8
    assert (await b.balance(Scope(org.tenant_id,org.subject_id[4:]),'tokens')).consumed.amount==0


async def test_organization_projection_shares_quota_but_separates_actor_replays():
    from controlplane_accounting import organization_scope
    from controlplane_accounting.organization import OrganizationBackend
    tenant='00000000-0000-4000-8000-000000000001';org='00000000-0000-4000-8000-000000000002'
    quota=organization_scope(tenant,org)
    raw=InternalBackend(RepositoryDouble(),authorize=lambda s,o:True)
    await raw.grant(Mutation(quota,'fund'),Q(10))
    refs=[]
    for actor in ('first','second'):
        scope=Scope(tenant,actor);bound=OrganizationBackend(raw,actor=scope,organization_id=org)
        q=await bound.quote(Mutation(scope,'same-quote-key'),'resource-'+actor,Q(4))
        r=await bound.reserve(Mutation(scope,'same-reserve-key'),q.quote_id,Q(4))
        await bound.commit(Mutation(scope,'same-commit-key'),r.reservation_id,Q(4))
        assert r.scope==scope
        refs.append(r.reservation_id)
        with pytest.raises(AccountingError):await bound.balance(Scope(tenant,'foreign'),'tokens')
    assert len(set(refs))==2
    assert (await raw.balance(quota,'tokens')).consumed.amount==8
    assert (await raw.balance(Scope(tenant,'first'),'tokens')).consumed.amount==0


async def test_org_entitlement_never_projects_another_actors_reference():
    from controlplane_accounting.organization import OrganizationBackend
    tenant='00000000-0000-4000-8000-000000000001'
    org='00000000-0000-4000-8000-000000000002'
    alice, bob = Scope(tenant,'alice'), Scope(tenant,'bob')
    raw=InternalBackend(RepositoryDouble(),authorize=lambda s,o:True)
    a=OrganizationBackend(raw,actor=alice,organization_id=org)
    b=OrganizationBackend(raw,actor=bob,organization_id=org)
    await raw.grant(Mutation(a.quota_scope,'grant'),Q(10))
    q=await a.quote(Mutation(alice,'quote'),'same-resource',Q(4))
    reservation=await a.reserve(Mutation(alice,'reserve'),q.quote_id,Q(4))
    with pytest.raises(AccountingError,match='unsupported'):
        await b.entitlement(bob,'same-resource')
    async def bob_binding(scope,resource):
        assert scope == bob and resource == 'same-resource'
        return 'bobs-own-reservation'
    b.entitlement_reference=bob_binding
    assert not (await b.entitlement(bob,'same-resource')).allowed
    async def alice_binding(scope,resource):
        assert scope == alice and resource == 'same-resource'
        return reservation.reservation_id
    a.entitlement_reference=alice_binding
    result=await a.entitlement(alice,'same-resource')
    assert result.allowed and result.scope==alice
    assert result.entitlement_id==reservation.reservation_id


async def test_reference_entitlement_selects_only_requested_allocation():
    b=backend()
    first=await funded(b,10)
    await b.grant(M('more'),Q(10))
    q=await b.quote(M('second-q'),'resource-a',Q(5))
    second=await b.reserve(M('second-r'),q.quote_id,Q(5))
    for r in (first,second):
        selected=await b.entitlement(S,'resource-a',reference=r.reservation_id)
        assert selected.entitlement_id==r.reservation_id and selected.quantity==r.quantity
    assert not (await b.entitlement(S,'other-resource',reference=first.reservation_id)).allowed
    await b.release(M('release-first'),first.reservation_id,Q(10))
    assert not (await b.entitlement(S,'resource-a',reference=first.reservation_id)).allowed
    assert (await b.entitlement(S,'resource-a',reference=second.reservation_id)).allowed


@pytest.mark.parametrize('support,wrong_reference',[(False,False),(True,False),(True,True)])
async def test_http_reference_entitlement_negotiates_additive_capability(support,wrong_reference):
    calls=[]
    async def handler(request):
        body=json.loads(request.content);calls.append(body)
        if request.url.path.endswith('/capabilities'):
            result={'scope':asdict(S),'units':['tokens'],'operations':['capabilities','entitlement']}
            if support:result['reference_entitlements']=True
            return httpx.Response(200,json=result)
        assert body['reference']=='owned-reference'
        return httpx.Response(200,json={'scope':asdict(S),'entitlement_id':'foreign' if wrong_reference else 'owned-reference',
            'resource':'resource-a','allowed':True,'expires_at':9999999999,'quantity':asdict(Q(2))})
    h=HttpBackend('https://accounting.example',token='unit',authorize=lambda s,o:True,units=('tokens',),transport=httpx.MockTransport(handler))
    try:
        if not support or wrong_reference:
            with pytest.raises(AccountingError,match='unsupported' if not support else 'invalid_request'):
                await h.entitlement(S,'resource-a',reference='owned-reference')
        else:
            assert (await h.entitlement(S,'resource-a',reference='owned-reference')).quantity==Q(2)
        assert len(calls)==(2 if support else 1)
    finally:await h.aclose()
