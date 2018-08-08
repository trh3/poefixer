"""
Back-end for currency price postprocessing.

The CurrencyPostprocessor class knows everything about tranforming
data in the stash and item tables as seen from the API into our
sales data and currency summaries.
"""


import time
import logging

import sqlalchemy

import poefixer
from .currency_names import \
    PRICE_RE, OFFICIAL_CURRENCIES, UNOFFICIAL_CURRENCIES


class CurrencyPostprocessor:
    """
    Take the sales and stash tables that represent very nearly as-is
    data from the API and start to crunch it down into some aggregates
    that represent the economy. This code is primarily responsible
    for tending the sale and currency_summary tables.
    """

    db = None
    start_time = None
    logger = None
    actual_currencies = {}

    def __init__(self, db, start_time, continuous=False, logger=logging):
        self.db = db
        self.start_time = start_time
        self.continuous = continuous
        self.logger = logger

    def get_actual_currencies(self):
        """Get the currencies in the DB and create abbreviation mappings"""

        def get_full_names():
            query = self.db.session.query(poefixer.CurrencySummary)
            query = query.add_columns(poefixer.CurrencySummary.from_currency)
            query = query.distinct()

            for row in query.all():
                yield row.from_currency

        def dashed(name):
            return name.replace(' ', '-')

        def dashed_clean(name):
            return dashed(name).replace("'", "")

        full_names = list(get_full_names())
        low = lambda name: name.lower()
        mapping = dict((low(name), name) for name in full_names)
        mapping.update(
            dict((dashed(low(name)), name) for name in full_names))
        mapping.update(
            dict((dashed_clean(low(name)), name) for name in full_names))

        self.logger.debug("Mapping of currencies: %r", mapping)

        return mapping

    def parse_note(self, note):
        """
        The 'note' is a user-edited field that sets pricing on an item or
        whole stash tab.

        Our goal is to parse out the sale price, if any, and return it or
        to returm None if there was no valid price.
        """

        if note is not None:
            match = PRICE_RE.search(note)
            if match:
                try:
                    (sale_type, amt, currency) = match.groups()
                    low_cur = currency.lower()
                    if '/' in amt:
                        num, den = amt.split('/', 1)
                        amt = float(num) / float(den)
                    else:
                        amt = float(amt)
                    if  low_cur in OFFICIAL_CURRENCIES:
                        return (amt, OFFICIAL_CURRENCIES[low_cur])
                    elif low_cur in UNOFFICIAL_CURRENCIES:
                        return (amt, UNOFFICIAL_CURRENCIES[low_cur])
                    elif low_cur in self.actual_currencies:
                        return (amt, self.actual_currencies[low_cur])
                    elif currency:
                        self.logger.warning(
                            "Currency note: %r has unknown currency abbrev %s",
                            note, currency)
                except ValueError as e:
                    # If float() fails it raises ValueError
                    if 'float' in str(e):
                        self.logger.debug("Invalid price: %r" % note)
                    else:
                        raise
        return (None, None)

    def _currency_query(self, start, block_size, offset):
        """
        Get a query from Item (linked to Stash) that have been updated since the
        last processed time given by `start`.

        Return a query that will fetch `block_size` rows starting at `offset`.
        """

        Item = poefixer.Item

        query = self.db.session.query(poefixer.Item, poefixer.Stash)
        query = query.add_columns(
            poefixer.Item.id,
            poefixer.Item.api_id,
            poefixer.Item.typeLine,
            poefixer.Item.note,
            poefixer.Item.updated_at,
            poefixer.Stash.stash,
            poefixer.Item.name,
            poefixer.Stash.public)
        query = query.filter(poefixer.Stash.id == poefixer.Item.stash_id)
        query = query.filter(poefixer.Item.active == True)
        query = query.filter(sqlalchemy.or_(
            sqlalchemy.and_(
                poefixer.Item.note != None,
                poefixer.Item.note != ""),
            sqlalchemy.and_(
                poefixer.Stash.stash != None,
                poefixer.Stash.stash != "")))
        query = query.filter(poefixer.Stash.public == True)
        #query = query.filter(sqlalchemy.func.json_contains_path(
        #    poefixer.Item.category, 'all', '$.currency') == 1)

        if start is not None:
            query = query.filter(poefixer.Item.updated_at >= start)

        # Tried streaming, but the result is just too large for that.
        query = query.order_by(
            Item.updated_at, Item.created_at, Item.id).limit(block_size)
        if offset:
            query = query.offset(offset)

        return query

    def _update_currency_pricing(
            self, name, currency, league, price, sale_time, is_currency):
        """
        Given a currency sale, update our understanding of what currency
        is now worth, and return the value of the sale in Chaos Orbs.
        """

        if is_currency:
            self._update_currency_summary(
                name, currency, league, price, sale_time)

        return self._find_value_of(currency, league, price)

    def _get_mean_and_std(
            self,
            name, currency, league, sale_time,
            restrict=False,
            mean=None,
            stddev=None):
        """
        For a given currency sale, get the weighted mean and standard deviation.

        Full returned value list is:

        * mean
        * standard deviation
        * total of all weights used
        * count of considered rows
        """

        # This may be DB-specific. Eventually getting it into a
        # pure-SQLAlchemy form would be good...
        weight_query = '''
                SELECT
                    sale2.id,
                    GREATEST(1, (
                        (1.0/GREATEST(1,(:now - sale2.updated_at))) * :unit)) as weight
                FROM sale as sale2'''

        if restrict:
            restrict_clause = '''ABS(%s.sale_amount - :mean) / 2.0 < :stddev'''
            weight_query += ' WHERE ' + (restrict_clause %  'sale2')
            restrict_clause = ' AND ' + (restrict_clause % 'sale')
            restrict_args = {
                'mean': mean,
                'stddev': stddev}
        else:
            restrict_clause = ''
            restrict_args = {}

        weighted_mean_select = sqlalchemy.sql.text('''
            SELECT
                SUM(wt.weight),
                SUM(sale.sale_amount * wt.weight)/GREATEST(1,SUM(wt.weight)) as mean,
                count(*) as rows
            FROM sale
                INNER JOIN item on sale.item_id = item.id
                INNER JOIN ('''+weight_query+''') as wt
                    ON wt.id = sale.id
            WHERE
                item.active = 1 AND
                item.league = :league AND
                sale.name = :name AND
                sale.sale_currency = :currency''' + restrict_clause)
        # Our weight unit is how long in seconds we should go before
        # beginning to decay a value. Decay is currently linear
        unit = 24*60*60
        weight, weighted_mean, count_rows = self.db.session.execute(
            weighted_mean_select, {
                'name': name,
                'currency': currency,
                'league': league,
                'now': sale_time,
                'unit': unit,
                **restrict_args}).fetchone()

        self.logger.debug(
            "Weighted mean sale of %s for %s %s",
            name, weighted_mean, currency)

        if weighted_mean is None or not count_rows:
            return None

        weighted_stddev_select = sqlalchemy.sql.text('''
            SELECT
                SQRT(
                    SUM(wt.weight * POW(sale.sale_amount - :weighted_mean, 2)) /
                        ((:count_rows * SUM(wt.weight)) / :count_rows)
                ) as weighted_stddev
            FROM sale
                INNER JOIN item on sale.item_id = item.id
                INNER JOIN ('''+weight_query+''') as wt
                    ON wt.id = sale.id
            WHERE
                item.active = 1 AND
                item.league = :league AND
                sale.name = :name AND
                sale.sale_currency = :currency''' + restrict_clause)
        weighted_stddev, = self.db.session.execute(
            weighted_stddev_select, {
                'name': name,
                'currency': currency,
                'league': league,
                'count_rows': count_rows,
                'weighted_mean': weighted_mean,
                'now': sale_time,
                'unit': unit,
                **restrict_args}).fetchone()

        return (weighted_mean, weighted_stddev, weight, count_rows)

    def _update_currency_summary(
            self, name, currency, league, price, sale_time):
        """Update the currency summary table with this new price"""

        query = self.db.session.query(poefixer.CurrencySummary)
        query = query.filter(poefixer.CurrencySummary.from_currency == name)
        query = query.filter(poefixer.CurrencySummary.to_currency == currency)
        query = query.filter(poefixer.CurrencySummary.league == league)
        do_update = query.one_or_none() is not None

        weighted_mean, weighted_stddev, weight, count = self._get_mean_and_std(
            name, currency, league, sale_time)
        self.logger.debug(
            "Weighted stddev of sale of %s in %s = %s",
            name, currency, weighted_stddev)
        if weighted_stddev is None:
            return None
        elif count > 3 and weighted_stddev > weighted_mean/2.0:
            self.logger.debug(
                "%s->%s: Large stddev=%s vs mean=%s, recalibrating",
                name, currency, weighted_stddev, weighted_mean)
            weighted_mean, weighted_stddev, weight, count2 = self._get_mean_and_std(
                name, currency, league, sale_time,
                restrict=True,
                mean=weighted_mean,
                stddev=weighted_stddev)
            self.logger.debug(
                "Recalibration ignored %s rows, final stddev=%s, mean=%s",
                count - count2, weighted_stddev, weighted_mean)
            count = count2


        if do_update:
            cmd = sqlalchemy.sql.expression.update(poefixer.CurrencySummary)
            cmd = cmd.where(
                poefixer.CurrencySummary.from_currency == name)
            cmd = cmd.where(
                poefixer.CurrencySummary.to_currency == currency)
            cmd = cmd.where(
                poefixer.CurrencySummary.league == league)
            add_values = {}
        else:
            cmd = sqlalchemy.sql.expression.insert(poefixer.CurrencySummary)
            add_values = {
                'from_currency': name,
                'to_currency': currency,
                'league': league}
        cmd = cmd.values(
            count=count,
            mean=weighted_mean,
            weight=weight,
            standard_dev=weighted_stddev, **add_values)
        self.db.session.execute(cmd)

    def _find_value_of(self, name, league, price):
        """
        Return the best current understanding of the value of the
        named currency, in chaos, in the given `league`,
        multiplied by the numeric `price`.

        Our primitive way of doing this for now is to say that the
        highest weighted conversion wins, presuming that that means
        the most stable sample, and we only try to follow the exchange
        to two levels down. Thus, we look for `X -> chaos` and
        `X -> Y -> chaos` and take whichever of those has the
        highest weighted sales (the weight of sales of
        `X -> Y -> chaos` being `min(weight(X->Y), weight(Y->chaos))`

        If all of that fails, we look for transactions going the other
        way (`chaos -> X`). This is less reliable, since it's a
        supply vs. demand side order, but if it's all we have, we
        roll with it.
        """

        if name == 'Chaos Orb':
            # The value of a chaos orb is always 1 chaos orb
            return price

        from_currency_field = poefixer.CurrencySummary.from_currency
        to_currency_field = poefixer.CurrencySummary.to_currency
        league_field = poefixer.CurrencySummary.league

        query = self.db.session.query(poefixer.CurrencySummary)
        query = query.filter(from_currency_field == name)
        query = query.filter(league_field == league)
        query = query.order_by(poefixer.CurrencySummary.weight.desc())
        high_score = None
        conversion = None
        for row in query.all():
            target = row.to_currency
            if target == 'Chaos Orb':
                if high_score and row.weight >= high_score:
                    self.logger.debug(
                        "Conversion discovered %s -> Chaos = %s",
                        name, row.mean)
                    high_score = row.weight
                    conversion = row.mean
                break
            query2 = self.db.session.query(poefixer.CurrencySummary)
            query2 = query2.filter(from_currency_field == target)
            query2 = query2.filter(to_currency_field == 'Chaos Orb')
            query2 = query2.filter(league_field == league)
            row2 = query2.one_or_none()
            if row2:
                score = min(row.weight, row2.weight)
                if (not high_score) or score > high_score:
                    high_score = score
                    conversion = row.mean * row2.mean
                    self.logger.debug(
                        "Conversion discovered %s -> %s (%s) -> Chaos (%s) = %s",
                        name, row2.from_currency, row.mean,
                        row2.mean, conversion)

        if high_score:
            return conversion * price
        else:
            query = self.db.session.query(poefixer.CurrencySummary)
            query = query.filter(from_currency_field == 'Chaos Orb')
            query = query.filter(to_currency_field == name)
            query = query.filter(league_field == league)
            row = query.one_or_none()
            if row:
                return (1.0 / row.mean) * price

        return None

    def _process_sale(self, row):
        if not (
                (row.Item.note and row.Item.note.startswith('~')) or
                row.Stash.stash.startswith('~')):
            self.logger.debug("No sale")
            return None
        is_currency = 'currency' in row.Item.category
        if is_currency:
            name = row.Item.typeLine
        else:
            name = (row.Item.name + " " + row.Item.typeLine).strip()
        pricing = row.Item.note
        stash_pricing = row.Stash.stash
        stash_price, stash_currency = self.parse_note(stash_pricing)
        price, currency = self.parse_note(pricing)
        if price is None:
            # No item price, so fall back to stash
            price, currency = (stash_price, stash_currency)
        if price is None or price == 0:
            self.logger.debug("No sale")
            return None
        self.logger.debug(
            "%s%sfor sale for %s %s" % (
                name,
                ("(currency) " if is_currency else ""),
                price, currency))
        existing = self.db.session.query(poefixer.Sale).filter(
            poefixer.Sale.item_id == row.Item.id).one_or_none()

        if not existing:
            existing = poefixer.Sale(
                item_id=row.Item.id,
                item_api_id=row.Item.api_id,
                name=name,
                is_currency=is_currency,
                sale_currency=currency,
                sale_amount=price,
                sale_amount_chaos=None,
                created_at=int(time.time()),
                item_updated_at=row.Item.updated_at,
                updated_at=int(time.time()))
        else:
            existing.sale_currency = currency
            existing.sale_amount = price
            existing.sale_amount_chaos = None
            existing.item_updated_at = row.Item.updated_at
            existing.updated_at = int(time.time())

        # Add it so we can re-calc values...
        self.db.session.add(existing)
        self.db.session.flush()

        league = row.Item.league

        amount_chaos = self._update_currency_pricing(
            name, currency, league, price, row.Item.updated_at, is_currency)

        if amount_chaos is not None:
            self.logger.debug(
                "Found chaos value of %s -> %s %s = %s",
                name, price, currency, amount_chaos)

            existing.sale_amount_chaos = amount_chaos
            self.db.session.merge(existing)

        return existing.id

    def get_last_processed_time(self):
        """
        Get the item update time relevant to the most recent sale
        record.
        """

        query = self.db.session.query(poefixer.Sale)
        query = query.order_by(poefixer.Sale.item_updated_at.desc()).limit(1)
        result = query.one_or_none()
        if result:
            reference_time = result.item_updated_at
            when = time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(reference_time))
            self.logger.debug(
                "Last processed sale for item: %s(%s)",
                result.item_id, when)
            return reference_time
        return None


    def do_currency_postprocessor(self):
        """Process all of the currency data we've seen to date."""

        def create_table(table, name):
            try:
                table.__table__.create(bind=self.db.session.bind)
            except sqlalchemy.exc.InternalError as e:
                if 'already exists' not in str(e):
                    raise
                self.logger.debug("%s table already exists.", name)
            else:
                self.logger.info("%s table created.", name)

        create_table(poefixer.Sale, "Sale")
        create_table(poefixer.CurrencySummary, "Currency Summary")

        prev = None
        while self.continuous:
            # Get all known currency names
            self.actual_currencies = self.get_actual_currencies()

            # Track what the most recently processed transaction was
            start = self.start_time or self.get_last_processed_time()
            if start:
                when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start))
                self.logger.info("Starting from %s", when)
            else:
                self.logger.info("Starting from beginning of item data.")

            # Actually process all outstading sale records
            (rows_done, last_row) = self._currency_processor_single_pass(start)

            # Pause if no processing was done
            if not prev or last_row != prev:
                prev = last_row
                self.logger.info("Processed %s rows in a pass", rows_done)
            elif self.continuous:
                time.sleep(1)

    def _currency_processor_single_pass(self, start):

        offset = 0
        count = 0
        all_processed = 0
        todo = True
        block_size = 1000 # Number of rows per block
        last_row = None

        while todo:
            query = self._currency_query(start, block_size, offset)

            # Stashes are named with a conventional pricing descriptor and
            # items can have a note in the same format. The price of an item
            # is the item price with the stash price as a fallback.
            count = 0
            for row in query.all():
                max_id = row.Item.id
                count += 1
                self.logger.debug("Row in %s" % row.Item.id)
                if count % 1000 == 0:
                    self.logger.info(
                        "%s rows in... (%s)",
                        count + offset, row.Item.updated_at)
                row_id = self._process_sale(row)
                if row_id:
                    last_row = row_id

            todo = count == block_size
            offset += count
            self.db.session.commit()
            all_processed += count

        return (all_processed, last_row)

# vim: et:sw=4:sts=4:ai:
