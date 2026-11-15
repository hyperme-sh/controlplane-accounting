from dataclasses import asdict
import json
from pathlib import Path
import pytest
from controlplane_accounting import (
    AccountingError, Quantity, Scope, Quote, Reservation, Mutation, MAX_AMOUNT,
    assert_scope, validate_quote, canonical_request, Capabilities,
)
from controlplane_accounting.wire import decode

SCOPE = Scope('tenant-a', 'subject-a')


@pytest.mark.parametrize('amount', [-1, MAX_AMOUNT + 1, True, False, 0.5, '1', None])
def test_non_integer_or_out_of_range(amount):
    with pytest.raises(AccountingError):
        Quantity(amount, 'tokens')


@pytest.mark.parametrize('amount', [0, 1, MAX_AMOUNT])
def test_boundary_quantities(amount):
    assert Quantity(amount, 'tokens').amount == amount


@pytest.mark.parametrize('scope', [Scope('tenant-b','subject-a'), Scope('tenant-a','subject-b')])
def test_reference_scope_confusion(scope):
    with pytest.raises(AccountingError, match='not_found'):
        assert_scope(scope, SCOPE)


def test_quote_expiry_and_exact_units():
    quote = Quote(SCOPE, 'quote-a', 'resource-a', Quantity(2,'tokens'), 100)
    validate_quote(quote, SCOPE, Quantity(2,'tokens'), 99)
    for now in (100,101):
        with pytest.raises(AccountingError, match='expired'):
            validate_quote(quote, SCOPE, quote.quantity, now)
    for quantity in (Quantity(2,'hours'), Quantity(1,'tokens')):
        with pytest.raises(AccountingError, match='conflict'):
            validate_quote(quote, SCOPE, quantity, 99)


def test_reserved_budget_cannot_be_overspent_or_converted():
    with pytest.raises(AccountingError, match='conflict'):
        Reservation(SCOPE,'r','q',Quantity(2,'tokens'),Quantity(2,'tokens'),Quantity(1,'tokens'),100)
    with pytest.raises(AccountingError, match='invalid_request'):
        Reservation(SCOPE,'r','q',Quantity(2,'tokens'),Quantity(0,'hours'),Quantity(0,'tokens'),100)


def test_replay_payload_binds_operation_scope_and_quantity():
    mutation = Mutation(SCOPE,'key')
    first = canonical_request('commit',mutation,reservation_id='r',quantity=Quantity(1,'tokens'))
    assert first == canonical_request('commit',mutation,quantity=Quantity(1,'tokens'),reservation_id='r')
    assert first != canonical_request('commit',mutation,reservation_id='r',quantity=Quantity(2,'tokens'))
    assert first != canonical_request('release',mutation,reservation_id='r',quantity=Quantity(1,'tokens'))
    assert first != canonical_request('commit',Mutation(Scope('b','b'),'key'),reservation_id='r',quantity=Quantity(1,'tokens'))


def test_older_client_accepts_additive_response_fields():
    quote = Quote(SCOPE,'q','resource',Quantity(0,'tokens'),100)
    body = asdict(quote)
    body['future_hint'] = {'not': 'required'}
    body['quantity']['future_hint'] = 1
    assert decode(Quote,body) == quote
    assert decode(Capabilities, {'scope':asdict(SCOPE),'units':['tokens'],
                                'operations':['quote']}).checkout is False
    del body['quote_id']
    with pytest.raises(AccountingError):
        decode(Quote,body)


def test_shared_vectors():
    vectors = json.loads((Path(__file__).parent / 'vectors.json').read_text())
    for vector in vectors:
        if vector['valid']:
            Quantity(**vector['quantity'])
        else:
            with pytest.raises(AccountingError):
                Quantity(**vector['quantity'])


def test_replay_returns_original_and_changed_payload_conflicts():
    from controlplane_accounting.contract import replay_result
    mutation = Mutation(SCOPE, 'retry')
    original = canonical_request('reserve', mutation, quote_id='q', quantity=Quantity(1, 'tokens'))
    result = {'reservation_id': 'original'}
    assert replay_result(original, result, original) is result
    changed = canonical_request('reserve', mutation, quote_id='q', quantity=Quantity(2, 'tokens'))
    with pytest.raises(AccountingError, match='conflict'):
        replay_result(original, result, changed)


def test_old_entitlement_is_decodable_but_has_no_quantity_authority():
    from controlplane_accounting import Entitlement
    body = dict(scope=asdict(SCOPE),entitlement_id='r',resource='item',allowed=True,expires_at=100)
    assert decode(Entitlement,body).quantity is None
    assert decode(Entitlement,dict(body,quantity={'amount':2,'unit':'access'})).quantity == Quantity(2,'access')
    with pytest.raises(AccountingError):decode(Entitlement,dict(body,quantity={'amount':True,'unit':'access'}))


def test_organization_principals_do_not_alias_actors_or_other_organizations():
    from controlplane_accounting import organization_scope
    tenant='00000000-0000-4000-8000-000000000001';org='00000000-0000-4000-8000-000000000002'
    shared=organization_scope(tenant,org)
    assert shared == Scope(tenant,'org:'+org)
    assert shared != Scope(tenant,org)
    assert shared != organization_scope(tenant,'00000000-0000-4000-8000-000000000003')
    with pytest.raises(AccountingError):organization_scope(tenant,'not-a-uuid')
