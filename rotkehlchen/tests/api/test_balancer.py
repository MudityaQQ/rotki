import random
import warnings as test_warnings
from contextlib import ExitStack
from http import HTTPStatus

import pytest
import requests

from rotkehlchen.accounting.structures.balance import Balance
from rotkehlchen.assets.asset import EvmToken, UnderlyingToken
from rotkehlchen.chain.ethereum.modules.balancer.types import (
    BalancerBPTEventType,
    BalancerEvent,
    BalancerPoolEventsBalance,
)
from rotkehlchen.chain.evm.types import string_to_evm_address
from rotkehlchen.constants import ZERO
from rotkehlchen.constants.assets import (
    A_BAL,
    A_COMP,
    A_LEND,
    A_LINK,
    A_MKR,
    A_WBTC,
    A_WETH,
    A_ZRX,
)
from rotkehlchen.fval import FVal
from rotkehlchen.tests.utils.api import (
    api_url_for,
    assert_error_response,
    assert_ok_async_response,
    assert_proper_response_with_result,
    wait_for_async_task,
)
from rotkehlchen.tests.utils.constants import A_BAND
from rotkehlchen.tests.utils.rotkehlchen import setup_balances
from rotkehlchen.types import (
    AssetAmount,
    ChainID,
    EvmTokenKind,
    Timestamp,
    deserialize_evm_tx_hash,
)

# Top holder of WBTC-WETH pool (0x1eff8af5d577060ba4ac8a29a13525bb0ee2a3d5)
BALANCER_TEST_ADDR1 = string_to_evm_address('0x49a2DcC237a65Cc1F412ed47E0594602f6141936')
BALANCER_TEST_ADDR2 = string_to_evm_address('0x7716a99194d758c8537F056825b75Dd0C8FDD89f')
BALANCER_TEST_ADDR2_POOL1 = EvmToken.initialize(
    address=string_to_evm_address('0x59A19D8c652FA0284f44113D0ff9aBa70bd46fB4'),
    chain_id=ChainID.ETHEREUM,
    token_kind=EvmTokenKind.ERC20,
    name='Balancer Pool Token',
    symbol='BPT',
    protocol='balancer',
    underlying_tokens=[
        UnderlyingToken(address=string_to_evm_address('0xba100000625a3754423978a60c9317c58a424e3D'), token_kind=EvmTokenKind.ERC20, weight=FVal(0.8)),  # noqa: E501  # BAL
        UnderlyingToken(address=string_to_evm_address('0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2'), token_kind=EvmTokenKind.ERC20, weight=FVal(0.2)),  # noqa: E501  # WETH
    ],
)
BALANCER_TEST_ADDR2_POOL2 = EvmToken.initialize(
    address=string_to_evm_address('0x574FdB861a0247401B317a3E68a83aDEAF758cf6'),
    chain_id=ChainID.ETHEREUM,
    token_kind=EvmTokenKind.ERC20,
    name='Balancer Pool Token',
    symbol='BPT',
    protocol='balancer',
    underlying_tokens=[
        UnderlyingToken(address=string_to_evm_address('0x0D8775F648430679A709E98d2b0Cb6250d2887EF'), token_kind=EvmTokenKind.ERC20, weight=FVal(0.1)),  # noqa: E501  # BAT
        UnderlyingToken(address=string_to_evm_address('0xdd974D5C2e2928deA5F71b9825b8b646686BD200'), token_kind=EvmTokenKind.ERC20, weight=FVal(0.1)),  # noqa: E501  # KNC
        UnderlyingToken(address=string_to_evm_address('0x80fB784B7eD66730e8b1DBd9820aFD29931aab03'), token_kind=EvmTokenKind.ERC20, weight=FVal(0.1)),  # noqa: E501  # LEND
        UnderlyingToken(address=string_to_evm_address('0x514910771AF9Ca656af840dff83E8264EcF986CA'), token_kind=EvmTokenKind.ERC20, weight=FVal(0.35)),  # noqa: E501  # LINK
        UnderlyingToken(address=string_to_evm_address('0x9f8F72aA9304c8B593d555F12eF6589cC3A579A2'), token_kind=EvmTokenKind.ERC20, weight=FVal(0.1)),  # noqa: E501  # MKR
        UnderlyingToken(address=string_to_evm_address('0xC011a73ee8576Fb46F5E1c5751cA3B9Fe0af2a6F'), token_kind=EvmTokenKind.ERC20, weight=FVal(0.1)),  # noqa: E501  # SNX
        UnderlyingToken(address=string_to_evm_address('0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2'), token_kind=EvmTokenKind.ERC20, weight=FVal(0.15)),  # noqa: E501  # WETH
    ],
)


@pytest.mark.parametrize('ethereum_accounts', [[BALANCER_TEST_ADDR1]])
@pytest.mark.parametrize('ethereum_modules', [['uniswap']])
@pytest.mark.parametrize('start_with_valid_premium', [True])
def test_get_balancer_module_not_activated(rotkehlchen_api_server):
    response = requests.get(
        api_url_for(rotkehlchen_api_server, 'evmmodulebalancesresource', module='balancer'),
    )
    assert_error_response(
        response=response,
        contained_in_msg='balancer module is not activated',
        status_code=HTTPStatus.CONFLICT,
    )


@pytest.mark.parametrize('ethereum_accounts', [[BALANCER_TEST_ADDR1]])
@pytest.mark.parametrize('ethereum_modules', [['balancer']])
@pytest.mark.parametrize('start_with_valid_premium', [True])
def test_get_balances(rotkehlchen_api_server, ethereum_accounts):
    """Test get the balances for premium users works as expected"""
    async_query = random.choice([False, True])
    rotki = rotkehlchen_api_server.rest_api.rotkehlchen
    setup = setup_balances(
        rotki,
        ethereum_accounts=ethereum_accounts,
        btc_accounts=None,
        original_queries=['zerion', 'logs', 'blocknobytime'],
    )
    with ExitStack() as stack:
        # patch ethereum/etherscan to not autodetect tokens
        setup.enter_ethereum_patches(stack)
        response = requests.get(api_url_for(
            rotkehlchen_api_server, 'evmmodulebalancesresource', module='balancer'),
            json={'async_query': async_query},
        )
        if async_query:
            task_id = assert_ok_async_response(response)
            outcome = wait_for_async_task(rotkehlchen_api_server, task_id)
            assert outcome['message'] == ''
            result = outcome['result']
        else:
            result = assert_proper_response_with_result(response)

    if len(result) != 1:
        test_warnings.warn(
            UserWarning(f'Test account {BALANCER_TEST_ADDR1} has no balances'),
        )
        return

    for pool_share in result[BALANCER_TEST_ADDR1]:
        assert pool_share['address'] is not None
        assert FVal(pool_share['total_amount']) >= ZERO
        assert FVal(pool_share['user_balance']['amount']) >= ZERO
        assert FVal(pool_share['user_balance']['usd_value']) >= ZERO

        for pool_token in pool_share['tokens']:
            assert pool_token['token'] is not None
            assert pool_token['total_amount'] is not None
            assert FVal(pool_token['user_balance']['amount']) >= ZERO
            assert FVal(pool_token['user_balance']['usd_value']) >= ZERO
            assert FVal(pool_token['usd_price']) >= ZERO
            assert FVal(pool_token['weight']) >= ZERO


BALANCER_TEST_ADDR2_EXPECTED_HISTORY_POOL1 = (
    BalancerPoolEventsBalance(
        address=BALANCER_TEST_ADDR2,
        pool_address_token=BALANCER_TEST_ADDR2_POOL1,
        profit_loss_amounts=[
            AssetAmount(FVal('0.744372160905819159')),
            AssetAmount(FVal('-0.039312851799093402')),
        ],
        usd_profit_loss=FVal('-0.76584117161052920880190053'),
        events=[
            BalancerEvent(
                tx_hash=deserialize_evm_tx_hash(
                    '0xb9dff9df4e3838c75d354d62c4596d94e5eb8904e07cee07a3b7ffa611c05544',
                ),
                log_index=331,
                address=BALANCER_TEST_ADDR2,
                timestamp=Timestamp(1597144247),
                event_type=BalancerBPTEventType.MINT,
                pool_address_token=BALANCER_TEST_ADDR2_POOL1,
                lp_balance=Balance(
                    amount=FVal('0.042569019597126949'),
                    usd_value=FVal('19.779488662371895'),
                ),
                amounts=[
                    AssetAmount(FVal('0')),
                    AssetAmount(FVal('0.05')),
                ],
            ),
            BalancerEvent(
                tx_hash=deserialize_evm_tx_hash(
                    '0xfa1dfeb83480e51a15137a93cb0eba9ac92c1b6b0ee0bd8551a422c1ed83695b',
                ),
                log_index=92,
                address=BALANCER_TEST_ADDR2,
                timestamp=Timestamp(1597243001),
                event_type=BalancerBPTEventType.BURN,
                pool_address_token=BALANCER_TEST_ADDR2_POOL1,
                lp_balance=Balance(
                    amount=FVal('0.042569019597126949'),
                    usd_value=FVal('19.01364749076136579119809947'),
                ),
                amounts=[
                    AssetAmount(FVal('0.744372160905819159')),
                    AssetAmount(FVal('0.010687148200906598')),
                ],
            ),
        ],
    )
)
BALANCER_TEST_ADDR2_EXPECTED_HISTORY_POOL2 = (
    BalancerPoolEventsBalance(
        address=BALANCER_TEST_ADDR2,
        pool_address_token=BALANCER_TEST_ADDR2_POOL2,
        profit_loss_amounts=[
            AssetAmount(FVal('0')),
            AssetAmount(FVal('0')),
            AssetAmount(FVal('0')),
            AssetAmount(FVal('0')),
            AssetAmount(FVal('0')),
            AssetAmount(FVal('0')),
            AssetAmount(FVal('-2.756044298156096352')),
        ],
        usd_profit_loss=FVal('-872.734395890491474835748575'),
        events=[
            BalancerEvent(
                tx_hash=deserialize_evm_tx_hash(
                    '0x256c042bf7d67a8b9e9566b8797335135015ab6e8d9196b1c39f5da7b8479006',
                ),
                log_index=171,
                address=BALANCER_TEST_ADDR2,
                timestamp=Timestamp(1598376244),
                event_type=BalancerBPTEventType.MINT,
                pool_address_token=BALANCER_TEST_ADDR2_POOL2,
                lp_balance=Balance(
                    amount=FVal('1289.21726317692448827'),
                    usd_value=FVal('3833.40'),
                ),
                amounts=[
                    AssetAmount(FVal('0')),
                    AssetAmount(FVal('0')),
                    AssetAmount(FVal('0')),
                    AssetAmount(FVal('0')),
                    AssetAmount(FVal('0')),
                    AssetAmount(FVal('0')),
                    AssetAmount(FVal('10')),
                ],
            ),
            BalancerEvent(
                tx_hash=deserialize_evm_tx_hash(
                    '0x6f9e6d5fd0562121ca4f695ffde661f5c184af421f68585be72ad59cfb8f881d',
                ),
                log_index=167,
                address=BALANCER_TEST_ADDR2,
                timestamp=Timestamp(1598377474),
                event_type=BalancerBPTEventType.BURN,
                pool_address_token=BALANCER_TEST_ADDR2_POOL2,
                lp_balance=Balance(
                    amount=FVal('1289.21726317692448827'),
                    usd_value=FVal('2960.665604109508525164251425'),
                ),
                amounts=[
                    AssetAmount(FVal('0')),
                    AssetAmount(FVal('0')),
                    AssetAmount(FVal('0')),
                    AssetAmount(FVal('0')),
                    AssetAmount(FVal('0')),
                    AssetAmount(FVal('0')),
                    AssetAmount(FVal('7.243955701843903648')),
                ],
            ),
        ],
    )
)
TEST_ADDR3_MOCKED_PRICES = {
    A_BAL.identifier: {
        'USD': {
            1597243001: FVal('20.104674263041243'),
        },
    },
    A_BAND.identifier: {
        'USD': {
            1597156065: FVal('14.466103356644934'),
            1597224640: FVal('12.534750403373085'),
        },
    },
    A_COMP.identifier: {
        'USD': {
            1597156065: FVal('176.4065022915484'),
            1597224640: FVal('218.51'),

        },
    },
    A_LEND.identifier: {
        'USD': {
            1597156065: FVal('0.39952667693410726'),
            1597224136: FVal('0.4026941951749709'),
        },
    },
    A_LINK.identifier: {
        'USD': {
            1597156065: FVal('13.379675286664355'),
            1597224062: FVal('13.080656699562843'),
        },
    },
    A_MKR.identifier: {
        'USD': {
            1597156065: FVal('624.6542090701207'),
            1597224640: FVal('591.9805247479154'),
        },
    },
    A_WBTC.identifier: {
        'USD': {
            1597156065: FVal('11865.846868426604'),
            1597224062: FVal('11851'),
        },
    },
    A_WETH.identifier: {
        'USD': {
            1597144247: FVal('395.5897732474379'),
            1597156065: FVal('395.5897732474379'),
            1597223901: FVal('387.19'),
            1597243001: FVal('378.7996188665494'),
            1598098652: FVal('395.46'),
            1598376244: FVal('383.34'),
            1598376468: FVal('408.7084082189914'),
            1598377474: FVal('408.7084082189914'),
        },
    },
    A_ZRX.identifier: {
        'USD': {
            1597156065: FVal('0.4791234716020489'),
            1597224640: FVal('0.4416470964397209'),
        },
    },
}


@pytest.mark.parametrize('ethereum_accounts', [[BALANCER_TEST_ADDR2]])
@pytest.mark.parametrize('ethereum_modules', [['balancer']])
@pytest.mark.parametrize('start_with_valid_premium', [True])
@pytest.mark.parametrize('mocked_price_queries', [TEST_ADDR3_MOCKED_PRICES])
@pytest.mark.parametrize('should_mock_price_queries', [True])
def test_get_events_history_1(
        rotkehlchen_api_server,
        ethereum_accounts,
        rotki_premium_credentials,  # pylint: disable=unused-argument
        start_with_valid_premium,  # pylint: disable=unused-argument
):
    """Test POOL1 (WETH-BAL) events balance for ADDR3"""
    async_query = random.choice([False, True])
    rotki = rotkehlchen_api_server.rest_api.rotkehlchen
    setup = setup_balances(
        rotki,
        ethereum_accounts=ethereum_accounts,
        btc_accounts=None,
        original_queries=['zerion', 'logs', 'blocknobytime'],
    )
    with ExitStack() as stack:
        # patch ethereum/etherscan to not autodetect tokens
        setup.enter_ethereum_patches(stack)
        response = requests.get(
            api_url_for(rotkehlchen_api_server, 'balancereventshistoryresource'),
            json={
                'async_query': async_query,
                'from_timestamp': 1597144247,
                'to_timestamp': 1597243001,
            },
        )
        if async_query:
            task_id = assert_ok_async_response(response)
            outcome = wait_for_async_task(rotkehlchen_api_server, task_id)
            assert outcome['message'] == ''
            result = outcome['result']
        else:
            result = assert_proper_response_with_result(response)

    address_pool_events_balances = result[BALANCER_TEST_ADDR2]

    assert len(address_pool_events_balances) == 2
    pool_event_balances = [
        pool_events_balance
        for pool_events_balance in address_pool_events_balances
        if pool_events_balance['pool_address'] == BALANCER_TEST_ADDR2_POOL1.evm_address
    ]

    assert len(pool_event_balances) == 1
    pool_events_balance = pool_event_balances[0]

    # check that the tokens were correctly created
    bpt_token = EvmToken(BALANCER_TEST_ADDR2_POOL1.identifier)
    assert bpt_token.name == 'Balancer Pool Token'
    assert bpt_token.symbol == 'BPT'
    assert bpt_token.decimals == 18

    assert pool_events_balance == BALANCER_TEST_ADDR2_EXPECTED_HISTORY_POOL1.serialize()


@pytest.mark.parametrize('ethereum_accounts', [[BALANCER_TEST_ADDR2]])
@pytest.mark.parametrize('ethereum_modules', [['balancer']])
@pytest.mark.parametrize('start_with_valid_premium', [True])
@pytest.mark.parametrize('mocked_price_queries', [TEST_ADDR3_MOCKED_PRICES])
@pytest.mark.parametrize('should_mock_price_queries', [True])
def test_get_events_history_2(
        rotkehlchen_api_server,
        ethereum_accounts,
        rotki_premium_credentials,  # pylint: disable=unused-argument
        start_with_valid_premium,  # pylint: disable=unused-argument
):
    """Test POOL2 (BAT-LINK-LEND-MKR-SNX-WETH-KNC) events balance for ADDR3"""
    async_query = random.choice([False, True])
    rotki = rotkehlchen_api_server.rest_api.rotkehlchen
    setup = setup_balances(
        rotki,
        ethereum_accounts=ethereum_accounts,
        btc_accounts=None,
        original_queries=['zerion', 'logs', 'blocknobytime'],
    )
    with ExitStack() as stack:
        # patch ethereum/etherscan to not autodetect tokens
        setup.enter_ethereum_patches(stack)
        response = requests.get(
            api_url_for(rotkehlchen_api_server, 'balancereventshistoryresource'),
            json={
                'async_query': async_query,
                'from_timestamp': 1598376244,
                'to_timestamp': 1598377474,
            },
        )
        if async_query:
            task_id = assert_ok_async_response(response)
            outcome = wait_for_async_task(rotkehlchen_api_server, task_id)
            assert outcome['message'] == ''
            result = outcome['result']
        else:
            result = assert_proper_response_with_result(response)

    address_pool_events_balances = result[BALANCER_TEST_ADDR2]

    assert len(address_pool_events_balances) == 2
    pool_event_balances = [
        pool_events_balance
        for pool_events_balance in address_pool_events_balances
        if pool_events_balance['pool_address'] == BALANCER_TEST_ADDR2_POOL2.evm_address
    ]

    assert len(pool_event_balances) == 1
    pool_events_balance = pool_event_balances[0]

    assert pool_events_balance == BALANCER_TEST_ADDR2_EXPECTED_HISTORY_POOL2.serialize()
