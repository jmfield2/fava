"""
The rationale behind this API is the following:

One day there will be a new module in beancount.report that returns all (for
beancount-web required) views as Python-dicts and -arrays, compatible with
JSON (so no datetime, etc.). Right now beancount.report does return data to
be displayed in a console, and HTML, and this (JSON) could be a third way of
"rendering" the data. These methods should be highly optimized for performance
and numerical correctness. If that one day really makes it's way into the
beancount-repo, then api.py is redundant and will be removed.

For the JSON-part: I want to keep all the returns in the API JSON-serializeable
(although they are called directly right now), because then, with very little
overhead, beancount-web could run on an external server and call into a local
bean-report.

Right now this module it is just a hacky placeholder for what could be in the
future, and therefore I only tried to get the numbers required, and did not
optimize for performance at all.
"""

import os
from datetime import date, timedelta

from beancount import loader
from beancount.core import compare, getters, interpolate, realization
from beancount.core.realization import RealAccount
from beancount.core.interpolate import compute_entries_balance
from beancount.core.account import has_component
from beancount.core.account_types import get_account_sign
from beancount.core.data import get_entry, posting_sortkey, Open, Close, Note,\
                                Document, Balance, Transaction, Pad, Event
from beancount.core.number import ZERO
from beancount.ops import prices, holdings, summarize
from beancount.parser import options
from beancount.query import query
from beancount.reports import context, holdings_reports
from beancount.utils import misc_utils

from beancount_web.util.dateparser import parse_date
from beancount_web.api.helpers import entries_in_inclusive_range,\
                                      holdings_at_dates
from beancount_web.api.serialization import serialize_inventory


class FilterException(Exception):
    pass


class BeancountReportAPI(object):
    def __init__(self, beancount_file_path):
        super(BeancountReportAPI, self).__init__()
        self.beancount_file_path = beancount_file_path
        self.filters = {
            'time': None,
            'tag': set(),
            'account': None,
            'payee': set(),
        }
        self.load_file()

    def load_file(self):
        """Load self.beancount_file_path and compute things that are independent
        of how the entries might be filtered later"""

        self.entries, self._errors, self.options = loader.load_file(self.beancount_file_path)
        self.all_entries = self.entries
        self.price_map = prices.build_price_map(self.all_entries)
        self.account_types = options.get_account_types(self.options)

        self.title = self.options['title']

        self.errors = []
        for error in self._errors:
            self.errors.append({
                'file': error.source['filename'],
                'line': error.source['lineno'],
                'error': error.message
            })

        self.active_years = list(getters.get_active_years(self.all_entries))
        self.active_tags = list(getters.get_all_tags(self.all_entries))
        self.active_payees = list(getters.get_all_payees(self.all_entries))
        self.apply_filters()

    def apply_filters(self):
        self.entries = self.all_entries

        if self.filters['time']:
            try:
                begin_date, end_date = parse_date(self.filters['time'])
                self.entries, _ = summarize.clamp_opt(self.entries, begin_date, end_date, self.options)
            except TypeError:
                raise FilterException('Failed to parse date string: {}'.format(self.filters['time']))

        if self.filters['tag']:
            self.entries = [entry
                            for entry in self.entries
                            if isinstance(entry, Transaction) and entry.tags and (entry.tags & set(self.filters['tag']))]

        if self.filters['payee']:
            self.entries = [entry
                            for entry in self.entries
                            if (isinstance(entry, Transaction) and entry.payee and (entry.payee in self.filters['payee']))
                            or (isinstance(entry, Transaction) and not entry.payee and ('' in self.filters['payee']))]

        if self.filters['account']:
            self.entries = [entry
                            for entry in self.entries
                            if isinstance(entry, Transaction) and
                                any(has_component(posting.account, self.filters['account'])
                                    for posting in entry.postings)]

        self.root_account = realization.realize(self.entries, self.account_types)
        self.all_accounts = self._account_components()
        self.all_accounts_leaf_only = self._account_components(leaf_only=True)

        self.closing_entries = summarize.cap_opt(self.entries, self.options)
        self.closing_real_accounts = realization.realize(self.closing_entries, self.account_types)

    def filter(self, **kwargs):
        changed = False
        for filter, current_value in self.filters.items():
            if filter in kwargs and kwargs[filter] != current_value:
                self.filters[filter] = kwargs[filter]
                changed = True

        if changed:
            self.apply_filters()

    def _account_components(self, leaf_only=False):
        # TODO rename
        """Gather all the account components available in the given directives.

        Args:
          entries: A list of directive instances.
        Returns:
            [
                {
                    'name': 'TV',
                    'full_name': 'Expenses:Tech:TV',
                    'depth': 3
                }, ...
            ]
        """
        accounts = []
        for child_account in realization.iter_children(self.root_account, leaf_only=leaf_only):
            accounts.append({
                'name': child_account.account.split(':')[-1],
                'full_name': child_account.account,
                'depth': child_account.account.count(':')+1,
            })

        return accounts[1:]

    def _table_tree(self, real_accounts):
        """
        Renders real_accounts and it's children as a flat list to be used
        in rendering tables.

        Returns:
            [
                {
                    'account': 'Expenses:Vacation',
                    'balances_children': {
                        'USD': 123.45, ...
                    },
                    'balances': {
                        'USD': 123.45, ...
                    },
                    'is_leaf': True,
                    'postings_count': 3
                }, ...
            ]
        """

        lines = []
        for real_account in realization.iter_children(real_accounts):
            line = {
                'account': real_account.account,
                'balances_children': self._table_totals(real_account),
                'balances': serialize_inventory(real_account.balance, at_cost=True),
                'is_leaf': len(real_account) == 0 or real_account.txn_postings,
                'postings_count': len(real_account.txn_postings)
            }
            lines.append(line)

        return lines

    def _table_totals(self, real_account):
        """
            Renders the total balances for root_acccounts and their children.

            Returns:
                {
                    'USD': 123.45,
                    ...
                }
        """
        return serialize_inventory(realization.compute_balance(real_account), at_cost=True)

    def _journal_for_postings(self, postings, include_types=None, with_change_and_balance=False):
        journal = []

        for posting, leg_postings, change, entry_balance in realization.iterate_with_balance(postings):

            if include_types and not isinstance(posting, include_types):
                continue

            entry = {
                'meta': {
                    'type': posting.__class__.__name__.lower(),
                    'filename': posting.meta['filename'],
                    'lineno': posting.meta['lineno']
                },
                'date': posting.date,
                'hash': compare.hash_entry(posting),
                'metadata': posting.meta.copy()
            }

            entry['metadata'].pop("__tolerances__", None)
            entry['metadata'].pop("filename", None)
            entry['metadata'].pop("lineno", None)

            if isinstance(posting, Open):
                entry['account']        = posting.account
                entry['currencies']     = posting.currencies

            if isinstance(posting, Close):
                entry['account']        = posting.account

            if isinstance(posting, Event):
                entry['type']           = posting.type
                entry['description']    = posting.description

            if isinstance(posting, Note):
                entry['comment']        = posting.comment

            if isinstance(posting, Document):
                entry['account']        = posting.account
                entry['filename']       = posting.filename

            if isinstance(posting, Pad):
                entry['account']        = posting.account
                entry['source_account'] = posting.source_account

            if isinstance(posting, Balance):
                entry['account']        = posting.account
                entry['change']         = { posting.amount.currency: posting.amount.number }
                entry['amount']         = { posting.amount.currency: posting.amount.number }

                if posting.diff_amount:
                    balance              = entry_balance.get_units(posting.amount.currency)
                    entry['diff_amount'] = { posting.diff_amount.currency: posting.diff_amount.number }
                    entry['balance']     = { balance.currency: balance.number }

            if isinstance(posting, Transaction):
                if posting.flag == 'P':
                    entry['meta']['type'] = 'padding'  # TODO handle Padding, Summarize and Transfer

                entry['flag']       = posting.flag
                entry['payee']      = posting.payee
                entry['narration']  = posting.narration
                entry['tags']       = posting.tags or []
                entry['links']      = posting.links or []
                entry['legs']       = [serialize_posting(p) for p in posting.postings]

            if with_change_and_balance:
                if isinstance(posting, Balance):
                    entry['change']     = { posting.amount.currency: posting.amount.number }
                    entry['balance']    = serialize_inventory(entry_balance)  #, include_currencies=entry['change'].keys())

                if isinstance(posting, Transaction):
                    entry['change']     = serialize_inventory(change)
                    entry['balance']    = serialize_inventory(entry_balance, include_currencies=entry['change'].keys())

            journal.append(entry)

        return journal

    def _interval_tuples(self, interval, entries):
        """
        Calculates tuples of (begin_date, end_date) of length interval for the period in
        which entries contains Transactions.

        Args:
            interval: Either 'month' or 'year'

        Returns:
            [
                (begin_date, end_date),
                ...
            ]
        """
        date_first, date_last = getters.get_min_max_dates(entries, (Transaction))

        if not date_first:
            return []

        def get_next_interval(date_, interval):
            if interval == 'year':
                return date(date_.year + 1, 1, 1)
            elif interval == 'month':
                month = (date_.month % 12) + 1
                year = date_.year + (date_.month + 1 > 12)
                return date(year, month, 1)
            else:
                raise NotImplementedError

        date_first = date(date_first.year, date_first.month, 1)
        date_last = get_next_interval(date_last, interval) - timedelta(days=1)

        interval_tuples = []
        while date_first <= date_last:
            interval_tuples.append((date_first, get_next_interval(date_first, interval) - timedelta(days=1)))
            date_first = get_next_interval(date_first, interval)

        return interval_tuples

    def monthly_income_expenses_totals(self):
        month_tuples = self._interval_tuples('month', self.entries)
        monthly_totals = []
        for begin_date, end_date in month_tuples:
            entries = entries_in_inclusive_range(self.entries, begin_date, end_date)
            realized = realization.realize(entries, self.account_types)
            income_totals = self._table_totals(realization.get(realized, self.account_types.income))
            expenses_totals = self._table_totals(realization.get(realized, self.account_types.expenses))

            monthly_totals.append({
                'begin_date': begin_date,
                'end_date': end_date,
                'income_totals': income_totals,
                'expenses_totals': expenses_totals
            })

        return monthly_totals

    def _interval_totals(self, account_name, interval, entries):
        """
        Renders totals for the active intervals in the entries

        Returns:
          [
              {
                  'begin_date': Date(...),    # TODO rename to date_begin
                  'end_date':   Date(...),    # TODO rename to date_end
                  'totals':     {
                                    'USD': 123.45,
                                }
              }, ...
          ]
        """

        interval_tuples = self._interval_tuples(interval, self.entries)
        interval_totals = []
        for begin_date, end_date in interval_tuples:
            totals = self.balances_totals(account_name, begin_date=begin_date, end_date=end_date)

            interval_totals.append({
                'begin_date': begin_date,
                'end_date': end_date,
                'totals': totals
            })

        return interval_totals

    def _real_accounts(self, account_name, entries, begin_date=None, end_date=None):
        """
        Returns the realization.RealAccount instances for account_name, and
        their entries clamped by the optional begin_date and end_date.

        Warning: For efficiency, the returned result does not include any added
        postings to account for balances at 'begin_date'.

        :return: realization.RealAccount instances
        """
        entries_in_range = entries_in_inclusive_range(entries, begin_date=begin_date, end_date=end_date)
        real_accounts = realization.get(realization.realize(entries_in_range, [account_name]), account_name)

        return real_accounts


    def balances(self, account_name, begin_date=None, end_date=None):
        """
        Renders account_name and it's children as a flat list to be used
        in rendering tables.

        Returns:
          [
              {
                  'account': 'Expenses:Vacation',
                  'balances_children': {
                      'USD': 123.45, ...
                  },
                  'balances': {
                      'USD': 123.45, ...
                  },
                  'is_leaf': True,
                  'postings_count': 3
              }, ...
          ]
        """
        real_accounts = self._real_accounts(account_name, self.entries, begin_date, end_date)

        return self._table_tree(real_accounts)


    def closing_balances(self, account_name, begin_date=None, end_date=None):
        real_accounts = self._real_accounts(account_name, self.closing_entries, begin_date, end_date)

        return self._table_tree(real_accounts)

    def balances_totals(self, account_name, begin_date=None, end_date=None):
        """
        Renders account_name and it's children as a flat list to be used
        in rendering tables.

        Returns:
            {
               'USD': 123.45,
            }
        """
        real_accounts = self._real_accounts(account_name, self.entries, begin_date, end_date)

        return self._table_totals(real_accounts)

    def monthly_balances(self, account_name):
        # TODO include balances_children
        # the account tree at time now

        account_names = [account['full_name'] for account in self.all_accounts if account['full_name'].startswith(account_name)]

        month_tuples = self._interval_tuples('month', self.entries)
        monthly_totals = { end_date.isoformat(): { currency: ZERO for currency in self.options['commodities']} for begin_date, end_date in month_tuples }

        arr = { account_name: {} for account_name in account_names }

        for begin_date, end_date in month_tuples:
            real_accounts = self._real_accounts(account_name, self.entries, begin_date=begin_date, end_date=end_date)

            _table_tree = self._table_tree(real_accounts)
            for line in _table_tree:
                arr[line['account']][end_date.isoformat()] = {
                    'balances': line['balances'],
                    'balances_children': line['balances_children']
                }

                if line['postings_count'] > 0:
                    for currency, number in line['balances'].items():
                        monthly_totals[end_date.isoformat()][currency] += number

        balances = sorted([
                        { 'account': account, 'totals': totals } for account, totals in arr.items()
                      ], key=lambda x: x['account'])

        return {
            'interval_end_dates': [end_date for begin_date, end_date in month_tuples],
            'balances': balances,
            'totals': monthly_totals
        }

    def yearly_balances(self, account_name):
        # TODO include balances_children
        # the account tree at time now

        account_names = [account['full_name'] for account in self.all_accounts if account['full_name'].startswith(account_name)]

        year_tuples = self._interval_tuples('year', self.entries)
        yearly_totals = { end_date.isoformat(): { currency: ZERO for currency in self.options['commodities']} for begin_date, end_date in year_tuples }

        arr = { account_name: {} for account_name in account_names }

        for begin_date, end_date in year_tuples:
            real_accounts = self._real_accounts(account_name, self.entries, begin_date=begin_date, end_date=end_date)

            _table_tree = self._table_tree(real_accounts)
            for line in _table_tree:
                arr[line['account']][end_date.isoformat()] = {
                    'balances': line['balances'],
                    'balances_children': line['balances_children']
                }

                if line['postings_count'] > 0:
                    for currency, number in line['balances'].items():
                        yearly_totals[end_date.isoformat()][currency] += number

        balances = sorted([
                        { 'account': account, 'totals': totals } for account, totals in arr.items()
                      ], key=lambda x: x['account'])

        return {
            'interval_end_dates': [end_date for begin_date, end_date in year_tuples],
            'balances': balances,
            'totals': yearly_totals
        }


    def trial_balance(self):
        return self._table_tree(self.root_account)[1:]

    def journal(self, account_name=None, with_change_and_balance=False, with_journal_children=True):
        if account_name:
            if not account_name in [account['full_name'] for account in self.all_accounts]:
                return []

            real_account = realization.get(self.root_account, account_name)

            if with_journal_children:
                postings = realization.get_postings(real_account)
            else:
                postings = []
                postings.extend(real_account.txn_postings)
                postings.sort(key=posting_sortkey)

            return self._journal_for_postings(postings, with_change_and_balance=with_change_and_balance)
        else:
            return self._journal_for_postings(self.entries, with_change_and_balance=with_change_and_balance)

    def documents(self):
        return self._journal_for_postings(self.entries, Document)

    def notes(self):
        return self._journal_for_postings(self.entries, Note)

    def events(self, event_type=None, only_include_newest=False):
        events = self._journal_for_postings(self.entries, Event)

        if event_type:
            events = [event for event in events if event['type'] == event_type]

        if only_include_newest:
            seen_types = list()
            for event in events:
                if not event['type'] in seen_types:
                    seen_types.append(event['type'])
            events = list({ event['type']: event for event in events }.values())

        return events

    def holdings(self):
        return holdings_reports.report_holdings(None, False, self.entries, self.options)

    def _net_worth_in_periods(self):
        month_tuples = self._interval_tuples('month', self.entries)
        monthly_totals = []
        end_dates = [p[1] + timedelta(days=1) for p in month_tuples]

        for (begin_date, end_date), holdings_list in zip(month_tuples,
                                                        holdings_at_dates(entries=self.entries,
                                                                          dates=end_dates,
                                                                          options_map=self.options,
                                                                          price_map=self.price_map)):
            totals = dict()
            for currency in self.options['operating_currency']:
                total = ZERO
                for holding in holdings.convert_to_currency(self.price_map, currency, holdings_list):
                    if holding.cost_currency == currency and holding.market_value:
                        total += holding.market_value
                if total != ZERO:
                    totals[currency] = total

            monthly_totals.append({
                'begin_date': begin_date,
                'end_date': end_date,
                'totals': totals
            })
        return monthly_totals

    def net_worth(self):
        monthly_totals = self._net_worth_in_periods()
        if monthly_totals:
            current = monthly_totals[-1]['totals']
        else:
            current = {}
        return {
            'net_worth': current,
            'monthly_totals': monthly_totals
        }

    def context(self, ehash=None):
        matching_entries = [entry
                                for entry in self.entries
                                if ehash == compare.hash_entry(entry)]

        contexts = []
        dcontext = self.options['dcontext']

        for entry in matching_entries:
            context_str = context.render_entry_context(
                self.entries, self.options, entry)

            hash_ = context_str.split("\n",2)[0].split(':')[1].strip()
            filenamelineno = context_str.split("\n",2)[1]
            filename = filenamelineno.split(":")[1].strip()
            lineno = int(filenamelineno.split(":")[2].strip())

            contexts.append({
                'hash': hash_,
                'context': context_str.split("\n",2)[2],
                'filename': filename,
                'line': lineno
            })

        # TODO
        #        if len(matching_entries) == 0:
        #            print("ERROR: Could not find matching entry for '{}'".format(ehash),
        #                  file=oss)
        #
        #        elif len(matching_entries) > 1:
        #            print("ERROR: Ambiguous entries for '{}'".format(ehash),
        #                  file=oss)
        #            print(file=oss)
        #            dcontext = app.options['dcontext']
        #            printer.print_entries(matching_entries, dcontext, file=oss)
        #
        #        else:

        return {
            'hash': ehash,
            'contexts': contexts,
            'journal': self._journal_for_postings(matching_entries)
        }

    def treemap_data(self, account_name):
        return {
            'label': account_name,
            'balances': self.balances(account_name),
            'modifier': get_account_sign(account_name, self.account_types),
        }

    def source_files(self):
        return list(set(
                        [self.beancount_file_path]
                        + [os.path.join(os.path.dirname(self.beancount_file_path), filename) for filename in self.options['include']]
                ))

    def source(self, file_path=None):
        if file_path:
            if file_path in self.source_files():
                with open(file_path, encoding='utf8') as f:
                    source_ = f.read()
                return source_
            else:
                return None  # TODO raise

        return self._source

    def set_source(self, file_path, source):
        if file_path in self.source_files():
            with open(file_path, 'w+', encoding='utf8') as f:
                f.write(source)
            return True
        else:
            return False  # TODO raise

    def monthly_totals(self, account_name):
        real_account = realization.get(self.root_account, account_name)
        return self._interval_totals(real_account.account, 'month', self.entries)

    def yearly_totals(self, account_name):
        real_account = realization.get(self.root_account, account_name)
        return self._interval_totals(real_account.account, 'year', self.entries)

    def commodities(self):
        return sorted(self.price_map.forward_pairs)

    def prices(self, base, quote):
        return prices.get_all_prices(self.price_map, "{}/{}".format(base, quote))

    def _activity_by_account(self, account_name=None):
        nb_activity_by_account = []
        for real_account in realization.iter_children(self.root_account):
            if not isinstance(real_account, RealAccount):
                continue
            if account_name and real_account.account != account_name:
                continue

            last_posting = realization.find_last_active_posting(
                real_account.txn_postings)

            if last_posting is None or isinstance(last_posting, Close):
                continue

            entry = get_entry(last_posting)

            nb_activity_by_account.append({
                'account': real_account.account,
                'last_posting_date': entry.date,
                'last_posting_filename': entry.meta['filename'],
                'last_posting_lineno': entry.meta['lineno']
            })

        return nb_activity_by_account

    def inventory(self, account_name):
        return compute_entries_balance(self.entries, prefix=account_name)

    def statistics(self, account_name=None):
        if account_name:
            activity_by_account = self._activity_by_account(account_name)
            return activity_by_account[0] if len(activity_by_account) == 1 else None
        else:
            # nb_entries_by_type
            entries_by_type = misc_utils.groupby(lambda entry: type(entry).__name__, self.entries)
            nb_entries_by_type = { name: len(entries) for name, entries in entries_by_type.items() }

            all_postings = [posting
                            for entry in self.entries
                            if isinstance(entry, Transaction)
                            for posting in entry.postings]

            # nb_postings_by_account
            postings_by_account = misc_utils.groupby(lambda posting: posting.account, all_postings)
            nb_postings_by_account = { key: len(postings) for key, postings in postings_by_account.items() }

            return {
                'entries_by_type':           nb_entries_by_type,
                'entries_by_type_total':     sum(nb_entries_by_type.values()),
                'postings_by_account':       nb_postings_by_account,
                'postings_by_account_total': sum(nb_postings_by_account.values()),
                'activity_by_account':       self._activity_by_account()
            }


    def is_valid_document(self, file_path):
        """Check if the given file_path is present in one of the
           Document entries or in a "statement"-metadata in a Transaction entry.

           :param file_path: A path to a file.
           :return: True when the file_path is refered to in a Document entry,
                    False otherwise.
        """
        is_present = False
        for entry in misc_utils.filter_type(self.entries, Document):
            if entry.filename == file_path:
                is_present = True

        if not is_present:
            for entry in misc_utils.filter_type(self.entries, Transaction):
                if 'statement' in entry.meta and entry.meta['statement'] == file_path:
                    is_present = True

        return is_present

    def query(self, bql_query_string):
        return query.run_query(self.entries, self.options, bql_query_string)

    def is_account_uptodate(self, account_name, look_back_days=60):
        """
        green:  if the latest posting is a balance check that passed (i.e., known-good)
        red:    if the latest posting is a balance check that failed (i.e., known-bad)
        yellow: if the latest posting is not a balance check (i.e., unknown)
        gray:   if the account hasn't been updated in a while (as compared to the last available date in the file)
        """
        journal = self.journal(account_name=account_name)
        if len(journal) == 0:
            return 'gray'
        last_entry = journal[-1]

        if last_entry['meta']['type'] == 'balance':
            if 'diff_amount' in last_entry:
                return 'red'
            else:
                return 'green'
        else:
            balance_entries = [entry for entry in journal if entry['meta']['type'] == 'balance']
            if len(balance_entries) == 0:
                return 'gray'
            last_balance_entry = balance_entries[-1]
            if last_balance_entry['date'] + timedelta(days=look_back_days) > last_entry['date']:
                return 'yellow'
            else:
                return 'gray'
