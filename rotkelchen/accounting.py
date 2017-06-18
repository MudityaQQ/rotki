from utils import tsToDate, ts_now
from order_formatting import (
    Events,
    BuyEvent,
    SellEvent,
    trade_get_other_pair,
    trade_get_assets,
    Trade,
    AssetMovement
)
from transactions import EthereumTransaction
from history import (
    NoPriceForGivenTimestamp,
    PriceQueryUnknownFromAsset,
    FIAT_CURRENCIES
)
from decimal import Decimal

YEAR_IN_SECONDS = 31536000  # 60 * 60 * 24 * 365


def action_get_timestamp(action):
    has_timestamp = (
        isinstance(action, Trade) or
        isinstance(action, AssetMovement) or
        isinstance(action, EthereumTransaction)
    )
    if has_timestamp:
        return action.timestamp

    # For loans and manual margin positions
    if 'close_time' not in action:
        print("----> {}".format(action))
    return action['close_time']


def action_get_type(action):
    if isinstance(action, Trade):
        return 'trade'
    elif isinstance(action, AssetMovement):
        return 'asset_movement'
    elif isinstance(action, EthereumTransaction):
        return 'ethereum_transaction'
    elif isinstance(action, dict):
        if 'btc_profit_loss' in action:
            return 'margin_position'
        return 'loan'
    else:
        raise ValueError('Unexpected action type found.')


class Accountant(object):

    def __init__(
            self,
            logger,
            price_historian,
            profit_currency,
            ignored_assets=['DAO']):

        self.log = logger
        self.price_historian = price_historian
        self.set_main_currency(profit_currency)
        self.ignored_assets = ignored_assets
        # If this flag is True when your asset is being forcefully sold as a
        # loan/margin settlement then profit/loss is also calculated before the entire
        # amount is taken as a loss
        self.count_profit_for_settlements = False

    def set_main_currency(self, currency):
        if currency not in FIAT_CURRENCIES:
            raise ValueError(
                'Attempted to set unsupported "{}" as main currency.'.format(currency)
            )

        self.profit_currency = currency

    def query_historical_price(self, from_asset, to_asset, timestamp):
        return self.price_historian.query_historical_price(from_asset, to_asset, timestamp)

    def get_rate_in_profit_currency(self, asset, timestamp):
        if asset == self.profit_currency:
            rate = 1
        else:
            rate = self.query_historical_price(
                asset,
                self.profit_currency,
                timestamp
            )
        return rate

    def add_buy_to_events(
            self,
            bought_asset,
            bought_amount,
            paid_with_asset,
            trade_rate,
            trade_fee,
            fee_currency,
            timestamp):

        paid_with_asset_rate = self.get_rate_in_profit_currency(paid_with_asset, timestamp)
        buy_rate = paid_with_asset_rate * trade_rate
        fee_price_in_profit_currency = 0
        if trade_fee != 0:
            fee_price_in_profit_currency = self.query_historical_price(
                fee_currency,
                self.profit_currency,
                timestamp
            )

        if bought_asset not in self.events:
            self.events[bought_asset] = Events(list(), list())

        fee_cost = fee_price_in_profit_currency * trade_fee
        cost = bought_amount * buy_rate + fee_cost
        self.events[bought_asset].buys.append(
            BuyEvent(
                amount=bought_amount,
                timestamp=timestamp,
                rate=buy_rate,
                fee_rate=fee_cost / bought_amount,
                cost=cost
            )
        )
        self.log.logdebug(
            'Buying {} "{}" for {} "{}" ({} "{}" per "{}" or {} "{}" per '
            '"{}") at {}'.format(
                bought_amount,
                bought_asset,
                bought_amount * trade_rate,
                paid_with_asset,
                trade_rate,
                paid_with_asset,
                bought_asset,
                buy_rate,
                self.profit_currency,
                bought_asset,
                tsToDate(timestamp, formatstr='%d/%m/%Y, %H:%M:%S')
            ))

    def add_loan_gain_to_events(
            self,
            gained_asset,
            gained_amount,
            fee_in_asset,
            timestamp):

        rate = self.get_rate_in_profit_currency(gained_asset, timestamp)

        if gained_asset not in self.events:
            self.events[gained_asset] = Events(list(), list())

        net_gain_amount = gained_amount - fee_in_asset
        gain_in_profit_currency = net_gain_amount * rate
        assert gain_in_profit_currency > 0, "Loan profit is negative. Should never happen"
        self.events[gained_asset].buys.append(
            BuyEvent(
                amount=net_gain_amount,
                timestamp=timestamp,
                rate=rate,
                fee_rate=0,
                cost=0
            )
        )
        # count profits if we are inside the query period
        if timestamp >= self.query_start_ts:
            self.loan_profit += gain_in_profit_currency

    def add_margin_positions_to_events(
            self,
            gained_asset,
            gained_amount,
            fee_in_asset,
            timestamp):

        rate = self.get_rate_in_profit_currency(gained_asset, timestamp)

        if gained_asset not in self.events:
            self.events[gained_asset] = Events(list(), list())

        net_gain_amount = gained_amount - fee_in_asset
        gain_in_profit_currency = net_gain_amount * rate
        print gain_in_profit_currency
        assert gain_in_profit_currency > 0, (
            'Margin profit is negative. Should never happen for the hacky way I use em now'
        )
        self.events[gained_asset].buys.append(
            BuyEvent(
                amount=net_gain_amount,
                timestamp=timestamp,
                rate=rate,
                fee_rate=0,
                cost=0
            )
        )
        # count profits if we are inside the query period
        if timestamp >= self.query_start_ts:
            self.margin_positions_profit += gain_in_profit_currency

    def add_asset_movement_to_events(self, category, asset, amount, timestamp, fee):
        rate = self.get_rate_in_profit_currency(asset, timestamp)
        self.asset_movement_fees += fee * rate
        if category == 'withdrawal':
            assert fee != 0, "So far all exchanges charge you for withdrawing"

    def account_for_gas_costs(self, transaction):

        if transaction.gas_price == -1:
            gas_price = self.last_gas_price
        else:
            gas_price = transaction.gas_price
            self.last_gas_price = transaction.gas_price

        # TODO When everything is decimal get rid of this conversion here
        rate = Decimal(self.get_rate_in_profit_currency('ETH', transaction.timestamp))
        eth_burned_as_gas = (transaction.gas_used * gas_price) / Decimal(10 ** 18)
        # print("from: {} to: {} block_number: {} hash: {} eth_burned_as_gas: {} EUR burned:{}".format(
        #     transaction.from_address, transaction.to_address, transaction.block_number, transaction.hash, eth_burned_as_gas, eth_burned_as_gas * rate
        # ))
        self.eth_transactions_gas_costs += eth_burned_as_gas * rate

    def add_buy_to_events_and_corresponding_sell(
            self,
            bought_asset,
            bought_amount,
            paid_with_asset,
            trade_rate,
            trade_fee,
            fee_currency,
            timestamp
    ):

        self.log.logdebug('\nBUY EVENT:')
        self.add_buy_to_events(
            bought_asset=bought_asset,
            bought_amount=bought_amount,
            paid_with_asset=paid_with_asset,
            trade_rate=trade_rate,
            trade_fee=trade_fee,
            fee_currency=fee_currency,
            timestamp=timestamp
        )

        if paid_with_asset not in FIAT_CURRENCIES:
            # then you are also selling some other asset to buy the bought asset
            try:
                bought_asset_rate_in_profit_currency = self.get_rate_in_profit_currency(
                    bought_asset,
                    timestamp
                )
            except (NoPriceForGivenTimestamp, PriceQueryUnknownFromAsset):
                bought_asset_rate_in_profit_currency = -1

            if bought_asset_rate_in_profit_currency != -1:
                with_bought_asset_gain = bought_asset_rate_in_profit_currency * bought_amount
                receiving_asset = bought_asset
                receiving_amount = bought_amount
                rate_in_profit_currency = bought_asset_rate_in_profit_currency / trade_rate
                gain_in_profit_currency = with_bought_asset_gain

            sold_amount = trade_rate * bought_amount
            sold_asset_rate_in_profit_currency = self.get_rate_in_profit_currency(
                paid_with_asset,
                timestamp
            )
            with_sold_asset_gain = sold_asset_rate_in_profit_currency * sold_amount

            # Consider as value of the sell what would give the least profit
            if (bought_asset_rate_in_profit_currency == -1 or
                    with_sold_asset_gain < with_bought_asset_gain):
                receiving_asset = self.profit_currency
                receiving_amount = with_sold_asset_gain
                trade_rate = sold_asset_rate_in_profit_currency
                rate_in_profit_currency = sold_asset_rate_in_profit_currency
                gain_in_profit_currency = with_sold_asset_gain

            # TODO: Here also check if the fee_currency is same as paid with asset
            #       and then add it to the sold_amount
            self.add_sell_to_events(
                selling_asset=paid_with_asset,
                selling_amount=sold_amount,
                receiving_asset=receiving_asset,
                receiving_amount=receiving_amount,
                # trade_rate=1 / trade_rate,
                trade_rate=trade_rate,
                rate_in_profit_currency=rate_in_profit_currency,
                gain_in_profit_currency=gain_in_profit_currency,
                total_fee_in_profit_currency=0,  # fee is done on the buy if at all
                timestamp=timestamp,
            )

    def search_buys_calculate_profit(self, selling_amount, selling_asset, timestamp):
        remaining_sold_amount = selling_amount
        stop_index = -1
        taxfree_bought_cost = 0
        taxable_bought_cost = 0
        taxable_amount = 0
        taxfree_amount = 0
        for idx, buy_event in enumerate(self.events[selling_asset].buys):
            sell_after_year = buy_event.timestamp + YEAR_IN_SECONDS < timestamp

            if remaining_sold_amount < buy_event.amount:
                stop_index = idx
                buying_cost = (
                    remaining_sold_amount * buy_event.rate +
                    buy_event.fee_rate * remaining_sold_amount
                )
                if sell_after_year:
                    taxfree_amount += remaining_sold_amount
                    taxfree_bought_cost += buying_cost
                else:
                    taxable_amount += remaining_sold_amount
                    taxable_bought_cost += buying_cost

                remaining_amount_from_last_buy = buy_event.amount - remaining_sold_amount
                self.log.logdebug(
                    '[{}] Using up {}/{} "{}" from the buy for {} "{}" per "{}"  at {}'.format(
                        'TAX-FREE' if sell_after_year else 'TAXABLE',
                        remaining_sold_amount,
                        buy_event.amount,
                        selling_asset,
                        buy_event.rate,
                        self.profit_currency,
                        selling_asset,
                        tsToDate(buy_event.timestamp, formatstr='%d/%m/%Y, %H:%M:%S')
                    ))
                # stop iterating since we found all buys to satisfy this sell
                break
            else:
                remaining_sold_amount -= buy_event.amount
                if sell_after_year:
                    taxfree_amount += buy_event.amount
                    taxfree_bought_cost += buy_event.cost
                else:
                    taxable_amount += buy_event.amount
                    taxable_bought_cost += buy_event.cost

                self.log.logdebug(
                    '[{}] Using up the entire buy of {} "{}" for {} "{}" per {} at {}'.format(
                        'TAX-FREE' if sell_after_year else 'TAXABLE',
                        buy_event.amount,
                        selling_asset,
                        buy_event.rate,
                        self.profit_currency,
                        selling_asset,
                        tsToDate(buy_event.timestamp, formatstr='%d/%m/%Y, %H:%M:%S')
                    ))

        if stop_index == -1:
            self.log.logalert('No documented buy found for "{}" before {}'.format(
                selling_asset, tsToDate(timestamp, formatstr='%d/%m/%Y, %H:%M:%S')
            ))
            # That means we had no documented buy for that asset. This is not good
            # because we can't prove a corresponding buy and as such we are burdened
            # calculating the entire sell as profit which needs to be taxed
            return selling_amount, 0, 0

        # Otherwise, delete all the used up buys from the list
        del self.events[selling_asset].buys[:stop_index]
        # and modify the amount of the buy where we stopped
        self.events[selling_asset].buys[0] = self.events[selling_asset].buys[0]._replace(
            amount=remaining_amount_from_last_buy
        )

        return taxable_amount, taxable_bought_cost, taxfree_bought_cost

    def add_sell_to_events(
            self,
            selling_asset,
            selling_amount,
            receiving_asset,
            receiving_amount,
            gain_in_profit_currency,
            total_fee_in_profit_currency,
            trade_rate,
            rate_in_profit_currency,
            timestamp,
            loan_settlement=False):

        if selling_asset not in self.events:
            self.events[selling_asset] = Events(list(), list())

        self.events[selling_asset].sells.append(
            SellEvent(
                amount=selling_amount,
                timestamp=timestamp,
                rate=rate_in_profit_currency,
                fee_rate=total_fee_in_profit_currency / selling_amount,
                gain=gain_in_profit_currency,
            )
        )

        if loan_settlement:
            self.log.logdebug('Loan Settlement Selling {} of "{}" for {} "{}" at {}'.format(
                selling_amount,
                selling_asset,
                gain_in_profit_currency,
                self.profit_currency,
                tsToDate(timestamp, formatstr='%d/%m/%Y, %H:%M:%S')
            ))
        else:
            self.log.logdebug(
                'Selling {} of "{}" for {} "{}" ({} "{}" per "{}" or {} "{}" '
                'per "{}") for total gain of {} "{}" at {}'.format(
                    selling_amount,
                    selling_asset,
                    receiving_amount,
                    receiving_asset,
                    trade_rate,
                    receiving_asset,
                    selling_asset,
                    rate_in_profit_currency,
                    self.profit_currency,
                    selling_asset,
                    gain_in_profit_currency,
                    self.profit_currency,
                    tsToDate(timestamp, formatstr='%d/%m/%Y, %H:%M:%S')
                ))

        # now search the buys for `paid_with_asset` and  calculate profit/loss
        taxable_amount, taxable_bought_cost, taxfree_bought_cost = self.search_buys_calculate_profit(
            selling_amount, selling_asset, timestamp
        )
        general_profit_loss = 0
        taxable_profit_loss = 0

        # and then calculate profit/loss
        if not loan_settlement or (loan_settlement and self.count_profit_for_settlements):
            taxable_gain = (
                rate_in_profit_currency * taxable_amount -
                total_fee_in_profit_currency * (taxable_amount / selling_amount)
            )

            general_profit_loss = gain_in_profit_currency - (
                taxfree_bought_cost +
                taxable_bought_cost
            )
            taxable_profit_loss = taxable_gain - taxable_bought_cost

        # should never happen, should be stopped at the main loop
        assert timestamp <= self.query_end_ts, (
            "Trade time > query_end_ts found in adding to sell event"
        )
        # count profit/losses if we are inside the query period
        if timestamp >= self.query_start_ts:
            if loan_settlement:
                self.settlement_losses += gain_in_profit_currency

            self.general_trade_profit_loss += general_profit_loss
            self.taxable_trade_profit_loss += taxable_profit_loss

    def add_sell_to_events_and_corresponding_buy(
            self,
            selling_asset,
            selling_amount,
            receiving_asset,
            receiving_amount,
            gain_in_profit_currency,
            total_fee_in_profit_currency,
            trade_rate,
            rate_in_profit_currency,
            timestamp):

        self.log.logdebug('\nSELL EVENT:')
        self.add_sell_to_events(
            selling_asset,
            selling_amount,
            receiving_asset,
            receiving_amount,
            gain_in_profit_currency,
            total_fee_in_profit_currency,
            trade_rate,
            rate_in_profit_currency,
            timestamp
        )

        if receiving_asset not in FIAT_CURRENCIES:
            # then you are also buying some other asset through your sell
            self.add_buy_to_events(
                bought_asset=receiving_asset,
                bought_amount=receiving_amount,
                paid_with_asset=selling_asset,
                # For polo corresponding buy to a sell you must not invert this
                trade_rate=1 / trade_rate,
                # trade_rate=trade_rate,
                trade_fee=0,  # fee should have already been acccounted on the sell side
                fee_currency=receiving_amount,  # does not matter
                timestamp=timestamp
            )

    def save_events(self):
        for asset, events in self.events.iteritems():
            pass

    def calculate_asset_details(self):
        """ Calculates what amount of all assets has been untouched for a year and
        is hence tax-free and also the average buy price for each asset"""
        self.details = dict()
        now = ts_now()
        for asset, events in self.events.iteritems():
            tax_free_amount_left = 0
            amount_sum = 0
            average = 0
            for buy_event in events.buys:
                if buy_event.timestamp + YEAR_IN_SECONDS < now:
                    tax_free_amount_left += buy_event.amount
                amount_sum += buy_event.amount
                average += buy_event.amount * buy_event.rate

            if amount_sum == 0:
                self.details[asset] = (0, 0)
            else:
                self.details[asset] = (tax_free_amount_left, average / amount_sum)

        return self.details

    def trade_add_to_sell_events(self, trade, loan_settlement):
        selling_asset = trade_get_other_pair(trade, trade.cost_currency)
        selling_asset_rate = self.get_rate_in_profit_currency(
            trade.cost_currency,
            trade.timestamp
        )
        selling_rate = selling_asset_rate * trade.rate
        fee_rate = self.query_historical_price(
            trade.fee_currency,
            self.profit_currency,
            trade.timestamp
        )
        total_sell_fee_cost = fee_rate * trade.fee
        gain_in_profit_currency = selling_rate * trade.amount - total_sell_fee_cost

        if not loan_settlement:
            self.add_sell_to_events_and_corresponding_buy(
                selling_asset=selling_asset,
                selling_amount=trade.amount,
                receiving_asset=trade.cost_currency,
                receiving_amount=trade.cost,
                gain_in_profit_currency=gain_in_profit_currency,
                total_fee_in_profit_currency=total_sell_fee_cost,
                trade_rate=trade.rate,
                rate_in_profit_currency=selling_rate,
                timestamp=trade.timestamp
            )
        else:
            self.add_sell_to_events(
                selling_asset=selling_asset,
                selling_amount=trade.amount,
                receiving_asset=None,
                receiving_amount=None,
                gain_in_profit_currency=gain_in_profit_currency,
                total_fee_in_profit_currency=total_sell_fee_cost,
                trade_rate=trade.rate,
                rate_in_profit_currency=selling_rate,
                timestamp=trade.timestamp,
                loan_settlement=True,
            )

    def process_history(self,
                        start_ts,
                        end_ts,
                        trade_history,
                        margin_history,
                        loan_history,
                        asset_movements,
                        eth_transactions):
        """Processes the entire history of cryptoworld actions in order to determine
        the price and time at which every asset was obtained and also
        the general and taxable profit/loss.
        """
        self.events = dict()
        self.general_trade_profit_loss = 0
        self.taxable_trade_profit_loss = 0
        self.settlement_losses = 0
        self.loan_profit = 0
        self.margin_positions_profit = 0
        self.last_gas_price = Decimal("2000000000")
        self.eth_transactions_gas_costs = 0
        self.asset_movement_fees = 0
        self.query_start_ts = start_ts
        self.query_end_ts = end_ts

        actions = list(trade_history)

        # If we got loans, we need to interleave them with the full history and re-sort
        if len(loan_history) != 0:
            actions.extend(loan_history)

        if len(asset_movements) != 0:
            actions.extend(asset_movements)

        if len(margin_history) != 0:
            actions.extend(margin_history)

        if len(eth_transactions) != 0:
            actions.extend(eth_transactions)

        actions.sort(
            key=lambda action: action_get_timestamp(action)
        )

        prev_time = 0
        for action in actions:

            # Assert we are sorted in ascending time order.
            timestamp = action_get_timestamp(action)
            assert timestamp >= prev_time, (
                "During history processing the trades/loans are not in ascending order"
            )
            prev_time = timestamp

            if timestamp > self.query_end_ts:
                break

            action_type = action_get_type(action)

            if action_type == 'loan':
                self.add_loan_gain_to_events(
                    gained_asset=action['currency'],
                    gained_amount=action['earned'],
                    fee_in_asset=action['fee'],
                    timestamp=timestamp,
                )
                continue
            elif action_type == 'asset_movement':
                self.add_asset_movement_to_events(
                    category=action.category,
                    asset=action.asset,
                    amount=action.amount,
                    timestamp=action.timestamp,
                    fee=action.fee
                )
                continue
            elif action_type == 'margin_position':
                self.add_margin_positions_to_events(
                    gained_asset='BTC',
                    gained_amount=action['btc_profit_loss'],
                    fee_in_asset=0,
                    timestamp=timestamp
                )
                continue
            elif action_type == 'ethereum_transaction':
                self.account_for_gas_costs(action)
                continue

            # if we get here it's a trade
            trade = action

            asset1, asset2 = trade_get_assets(trade)
            if asset1 in self.ignored_assets or asset2 in self.ignored_assets:
                self.log.logdebug("Ignoring trade with {} {}".format(asset1, asset2))
                continue

            # When you buy, you buy with the cost_currency and receive the other one
            # When you sell, you sell the amount in non-cost_currency and receive
            # costs in cost_currency
            if trade.type == 'buy':
                self.add_buy_to_events_and_corresponding_sell(
                    bought_asset=trade_get_other_pair(trade, trade.cost_currency),
                    bought_amount=trade.amount,
                    paid_with_asset=trade.cost_currency,
                    trade_rate=trade.rate,
                    trade_fee=trade.fee,
                    fee_currency=trade.fee_currency,
                    timestamp=trade.timestamp
                )
            elif trade.type == 'sell':
                self.trade_add_to_sell_events(trade, False)
            elif trade.type == 'settlement_sell':
                # in poloniex settlements sell some asset to get BTC to repay a loan
                self.trade_add_to_sell_events(trade, True)
            elif trade.type == 'settlement_buy':
                # in poloniex settlements you buy some asset with BTC to repay a loan
                # so in essense you sell BTC to repay the loan
                selling_asset = 'BTC'
                selling_asset_rate = self.get_rate_in_profit_currency(
                    selling_asset,
                    trade.timestamp
                )
                selling_rate = selling_asset_rate * trade.rate
                fee_rate = self.query_historical_price(
                    trade.fee_currency,
                    self.profit_currency,
                    trade.timestamp
                )
                total_sell_fee_cost = fee_rate * trade.fee
                gain_in_profit_currency = selling_rate * trade.amount - total_sell_fee_cost
                self.add_sell_to_events(
                    selling_asset=selling_asset,
                    selling_amount=trade.cost,
                    receiving_asset=None,
                    receiving_amount=None,
                    gain_in_profit_currency=gain_in_profit_currency,
                    total_fee_in_profit_currency=total_sell_fee_cost,
                    trade_rate=trade.rate,
                    rate_in_profit_currency=selling_rate,
                    timestamp=trade.timestamp,
                    loan_settlement=True
                )
            else:
                raise ValueError('Unknown trade type "{}" encountered'.format(trade.type))

        self.calculate_asset_details()

        # TODO: When everything is decimal, get rid of the conversions here
        sum_other_actions = (
            Decimal(self.margin_positions_profit) +
            Decimal(self.loan_profit) -
            Decimal(self.settlement_losses) -
            Decimal(self.asset_movement_fees) -
            Decimal(self.eth_transactions_gas_costs)
        )
        return {
            'loan_profit': self.loan_profit,
            'margin_positions_profit': self.margin_positions_profit,
            'settlement_losses': self.settlement_losses,
            'ethereum_transaction_gas_costs': self.eth_transactions_gas_costs,
            'asset_movement_fees': self.asset_movement_fees,
            'general_trade_profit_loss': self.general_trade_profit_loss,
            'taxable_trade_profit_loss': self.taxable_trade_profit_loss,
            'total_taxable_profit_loss': Decimal(self.taxable_trade_profit_loss) + sum_other_actions,
            'total_profit_loss': Decimal(self.general_trade_profit_loss) + sum_other_actions,
        }
