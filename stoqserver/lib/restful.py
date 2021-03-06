# -*- coding: utf-8 -*-
# vi:si:et:sw=4:sts=4:ts=4

##
## Copyright (C) 2018 Async Open Source <http://www.async.com.br>
## All rights reserved
##
## This program is free software; you can redistribute it and/or
## modify it under the terms of the GNU Lesser General Public License
## as published by the Free Software Foundation; either version 2
## of the License, or (at your option) any later version.
##
## This program is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU Lesser General Public License for more details.
##
## You should have received a copy of the GNU Lesser General Public License
## along with this program; if not, write to the Free Software
## Foundation, Inc., or visit: http://www.gnu.org/.
##
## Author(s): Stoq Team <stoq-devel@async.com.br>
##

import base64
import contextlib
import datetime
import decimal
import functools
import json
import logging
import os
import pickle
import psycopg2
from queue import Queue
from threading import Event
import uuid
import io
import select
import time
from hashlib import md5

from kiwi.component import provide_utility
from kiwi.currency import currency
from flask import Flask, request, session, abort, send_file, make_response, Response
from flask_restful import Api, Resource

from stoqlib.api import api
from stoqlib.database.runtime import get_current_station
from stoqlib.database.interfaces import ICurrentUser
from stoqlib.domain.events import SaleConfirmedRemoteEvent
from stoqlib.domain.image import Image
from stoqlib.domain.payment.group import PaymentGroup
from stoqlib.domain.payment.method import PaymentMethod
from stoqlib.domain.payment.card import CreditCardData, CreditProvider, CardPaymentDevice
from stoqlib.domain.payment.payment import Payment
from stoqlib.domain.person import LoginUser, Person, Client, ClientCategory
from stoqlib.domain.product import Product
from stoqlib.domain.sale import Sale
from stoqlib.domain.sellable import (Sellable, SellableCategory,
                                     ClientCategoryPrice)
from stoqlib.domain.till import Till, TillSummary
from stoqlib.exceptions import LoginError
from stoqlib.lib.configparser import get_config
from stoqlib.lib.dateutils import (INTERVALTYPE_MONTH, create_date_interval,
                                   localnow)
from stoqlib.lib.formatters import raw_document
from stoqlib.lib.osutils import get_application_dir
from stoqlib.lib.translation import dgettext
from stoqlib.lib.threadutils import threadit
from stoqlib.lib.pluginmanager import get_plugin_manager
from storm.expr import Desc, LeftJoin, Join

_ = lambda s: dgettext('stoqserver', s)

try:
    from stoqntk.ntkapi import Ntk, NtkException, PwInfo
    from stoqntk.ntkenums import PwDat
    # The ntk lib instance.
    has_ntk = True
except ImportError:
    has_ntk = False
    ntk = None

_last_gc = None
_expire_time = datetime.timedelta(days=1)
_session = None
log = logging.getLogger(__name__)

TRANSPARENT_PIXEL = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII='  # nopep8

WORKERS = []


def _get_user_hash():
    return md5(
        api.sysparam.get_string('USER_HASH').encode('UTF-8')).hexdigest()


@contextlib.contextmanager
def _get_session():
    global _session
    global _last_gc

    # Indexing some session data by the USER_HASH will help to avoid
    # maintaining sessions between two different databases. This could lead to
    # some errors in the POS in which the user making the sale does not exist.
    session_file = os.path.join(
        get_application_dir(), 'session-{}.db'.format(_get_user_hash()))
    if os.path.exists(session_file):
        with open(session_file, 'rb') as f:
            try:
                _session = pickle.load(f)
            except Exception:
                _session = {}
    else:
        _session = {}

    # From time to time remove old entries from the session dict
    now = localnow()
    if now - (_last_gc or datetime.datetime.min) > _expire_time:
        for k, v in list(_session.items()):
            if now - v['date'] > _expire_time:
                del _session[k]
        _last_gc = localnow()

    yield _session

    with open(session_file, 'wb') as f:
        pickle.dump(_session, f)


def _login_required(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        session_id = request.headers.get('stoq-session', None)
        if session_id is None:
            abort(401, 'No session id provided in header')

        with _get_session() as s:
            session_data = s.get(session_id, None)
            if session_data is None:
                abort(401, 'Session does not exist')

            if localnow() - session_data['date'] > _expire_time:
                abort(401, 'Session expired')

            # Refresh last date to avoid it expiring while being used
            session_data['date'] = localnow()
            session['user_id'] = session_data['user_id']
            with api.new_store() as store:
                user = store.get(LoginUser, session['user_id'])
                provide_utility(ICurrentUser, user, replace=True)

        return f(*args, **kwargs)

    return wrapper


def _store_provider(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        with api.new_store() as store:
            try:
                return f(store, *args, **kwargs)
            except Exception as e:
                store.retval = False
                abort(500, str(e))

    return wrapper


def worker(f):
    """A marker for a function that should be threaded when the server executes.

    Usefull for regular checks that should be made on the server that will require warning the
    client
    """
    WORKERS.append(f)
    return f


class _BaseResource(Resource):

    routes = []

    def get_arg(self, attr, default=None):
        """Get the attr from querystring, form data or json"""
        # This is not working on all versions.
        #if request.is_json:
        if request.get_json():
            return request.get_json().get(attr, None)

        return request.form.get(attr, request.args.get(attr, default))

    def test_printer(self):
        # Test the printer to see if its working properly.
        printer = None
        try:
            printer = api.device_manager.printer
            printer and printer.is_drawer_open()
        except Exception:
            if printer:
                printer._port.close()
            api.device_manager._printer = None
            for i in range(20):
                log.info('Printer check failed. Reopening')
                try:
                    printer = api.device_manager.printer
                    printer.is_drawer_open()
                    break
                except Exception:
                    time.sleep(1)
            else:
                raise

            manager = get_plugin_manager()
            # Invalidate the printer in the sat plugin so that it re-opens it
            manager.get_plugin('sat').ui.printer = None
            nonfiscal = manager.get_plugin('nonfiscal')
            if nonfiscal and nonfiscal.ui:
                nonfiscal.ui.printer = printer


class DataResource(_BaseResource):
    """All the data the POS needs RESTful resource."""

    routes = ['/data']
    method_decorators = [_login_required, _store_provider]

    # All the tables get_data uses (directly or indirectly)
    watch_tables = ['sellable', 'product', 'storable', 'product_stock_item', 'branch_station',
                    'branch', 'login_user', 'sellable_category', 'client_category_price',
                    'payment_method', 'credit_provider']

    @worker
    def _postgres_listen():
        store = api.new_store()
        conn = store._connection._raw_connection
        conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
        cursor = store._connection.build_raw_cursor()
        cursor.execute("LISTEN update_te;")

        message = False
        while True:
            if select.select([conn], [], [], 5) != ([], [], []):
                conn.poll()
                while conn.notifies:
                    notify = conn.notifies.pop(0)
                    te_id, table = notify.payload.split(',')
                    # Update the data the client has when one of those changes
                    message = message or table in DataResource.watch_tables

            if message:
                EventStream.put({
                    'type': 'SERVER_UPDATE_DATA',
                    'data': DataResource.get_data(store)
                })
                message = False

    @classmethod
    def _get_categories(cls, store):
        categories_root = []
        aux = {}
        # SellableCategory and Sellable/Product data
        for c in store.find(SellableCategory):
            if c.category_id is None:
                parent_list = categories_root
            else:
                parent_list = aux.setdefault(
                    c.category_id, {}).setdefault('children', [])

            c_dict = aux.setdefault(c.id, {})
            parent_list.append(c_dict)

            # Set/Update the data
            c_dict.update({
                'id': c.id,
                'description': c.description,
            })
            c_dict.setdefault('children', [])
            products_list = c_dict.setdefault('products', [])

            tables = [Sellable, LeftJoin(Product, Product.id == Sellable.id)]
            sellables = store.using(*tables).find(
                Sellable, category=c, status='available').order_by('height', 'description')
            for s in sellables:
                ccp = store.find(ClientCategoryPrice, sellable_id=s.id)
                ccp_dict = {}
                for item in ccp:
                    ccp_dict[item.category_id] = str(item.price)

                products_list.append({
                    'id': s.id,
                    'description': s.description,
                    'price': str(s.price),
                    'order': str(s.product.height),
                    'category_prices': ccp_dict,
                    'color': s.product.part_number,
                    'availability': (
                        s.product and s.product.storable and
                        {
                            si.branch.id: str(si.quantity)
                            for si in s.product.storable.get_stock_items()
                        }
                    )
                })

            aux[c.id] = c_dict
        return categories_root

    @classmethod
    def _get_payment_methods(self, store):
        # PaymentMethod data
        payment_methods = []
        for pm in PaymentMethod.get_active_methods(store):
            if not pm.selectable():
                continue

            data = {'name': pm.method_name,
                    'max_installments': pm.max_installments}
            if pm.method_name == 'card':
                # FIXME: Add voucher
                data['card_types'] = [CreditCardData.TYPE_CREDIT,
                                      CreditCardData.TYPE_DEBIT]

            payment_methods.append(data)

        return payment_methods

    @classmethod
    def _get_card_providers(self, store):
        providers = []
        for i in CreditProvider.get_card_providers(store):
            providers.append({'short_name': i.short_name, 'provider_id': i.provider_id})

        return providers

    @classmethod
    def get_data(cls, store):
        """Returns all data the POS needs to run

        This includes:

        - Which branch and statoin he is operating for
        - Current loged in user
        - What categories it has
            - What sellables those categories have
                - The stock amount for each sellable (if it controls stock)
        """
        station = get_current_station(store)
        user = api.get_current_user(store)
        staff_category = store.find(ClientCategory, ClientCategory.name == 'Staff').one()

        # Current branch data
        retval = dict(
            branch=api.get_current_branch(store).id,
            branch_station=station.name,
            user=user and user.username,
            categories=cls._get_categories(store),
            payment_methods=cls._get_payment_methods(store),
            providers=cls._get_card_providers(store),
            staff_id=staff_category.id if staff_category else None,
        )

        return retval

    def get(self, store):
        return self.get_data(store)


class PrinterException(Exception):
    pass


class DrawerResource(_BaseResource):
    """Drawer RESTful resource."""

    routes = ['/drawer']
    method_decorators = [_login_required]

    @classmethod
    def _open_drawer(cls):
        if not api.device_manager.printer:
            raise PrinterException('Printer not configured in this station')
        api.device_manager.printer.open_drawer()

    @classmethod
    def _is_open(cls):
        try:
            if not api.device_manager.printer:
                return False
            return api.device_manager.printer.is_drawer_open()
        except Exception:
            return False

    @classmethod
    @worker
    def check_drawer_loop():
        is_open = DrawerResource._is_open()

        # Check every second if it is opened.
        # Alert only if changes.
        while True:
            if not is_open and DrawerResource._is_open():
                is_open = True
                EventStream.put({
                    'type': 'DRAWER_ALERT_OPEN',
                })
            elif is_open and not DrawerResource._is_open():
                is_open = False
                EventStream.put({
                    'type': 'DRAWER_ALERT_CLOSE',
                })
            time.sleep(1)

    def get(self):
        """Get the current status of the drawer"""
        return self._is_open()

    def post(self):
        """Send a signal to open the drawer"""
        try:
            self._open_drawer()
        except Exception as e:
            raise PrinterException('Could not proceed with the operation. Reason: ' + str(e))
        return 'success', 200


class PingResource(_BaseResource):
    """Ping RESTful resource."""

    routes = ['/ping']

    def get(self):
        return 'pong from stoqserver'


def format_cpf(document):
    return '%s.%s.%s-%s' % (document[0:3], document[3:6], document[6:9],
                            document[9:11])


def format_cnpj(document):
    return '%s.%s.%s/%s-%s' % (document[0:2], document[2:5], document[5:8],
                               document[8:12], document[12:])


def format_document(document):
    if len(document) == 11:
        return format_cpf(document)
    else:
        return format_cnpj(document)


class TillResource(_BaseResource):
    """Till RESTful resource."""
    routes = ['/till']
    method_decorators = [_login_required]

    def _open_till(self, store, initial_cash_amount=0):
        station = get_current_station(store)
        last_till = Till.get_last(store)
        if not last_till or last_till.status != Till.STATUS_OPEN:
            # Create till and open
            till = Till(store=store, station=station)
            till.open_till()
            till.initial_cash_amount = decimal.Decimal(initial_cash_amount)
        else:
            # Error, till already opened
            assert False

    def _close_till(self, store, till_summaries):
        self.test_printer()
        # Here till object must exist
        till = Till.get_last(store)

        # Create TillSummaries
        till.get_day_summary()

        # Find TillSummary and store the user_value
        for till_summary in till_summaries:
            method = PaymentMethod.get_by_name(store, till_summary['method'])

            if till_summary['provider']:
                provider = store.find(CreditProvider, short_name=till_summary['provider']).one()
                summary = TillSummary.get_or_create(store, till=till, method=method.id,
                                                    provider=provider.id,
                                                    card_type=till_summary['card_type'])
            # Money method has no card_data or provider
            else:
                summary = TillSummary.get_or_create(store, till=till, method=method.id)

            summary.user_value = decimal.Decimal(till_summary['user_value'])

        balance = till.get_balance()
        till.add_debit_entry(balance, _('Blind till closing'))
        till.close_till()

    def _add_credit_or_debit_entry(self, store, data):
        # Here till object must exist
        till = Till.get_last(store)
        user = store.get(LoginUser, session['user_id'])

        # FIXME: Check balance when removing to prevent negative till.
        if data['operation'] == 'debit_entry':
            reason = _('The user %s removed cash from till') % user.username
            till.add_debit_entry(decimal.Decimal(data['entry_value']), reason)
        elif data['operation'] == 'credit_entry':
            reason = _('The user %s supplied cash to the till') % user.username
            till.add_credit_entry(decimal.Decimal(data['entry_value']), reason)

    def _get_till_summary(self, store, till):
        payment_data = []
        for summary in till.get_day_summary():
            payment_data.append({
                'method': summary.method.method_name,
                'provider': summary.provider.short_name if summary.provider else None,
                'card_type': summary.card_type,
                'system_value': str(summary.system_value),
            })

        # XXX: We shouldn't create TIllSummaries since we are not closing the Till,
        # so we must rollback.
        store.rollback(close=False)

        return payment_data

    def post(self):
        data = request.get_json()
        with api.new_store() as store:
            # Provide responsible
            if data['operation'] == 'open_till':
                self._open_till(store, data['initial_cash_amount'])
            elif data['operation'] == 'close_till':
                self._close_till(store, data['till_summaries'])
            elif data['operation'] in ['debit_entry', 'credit_entry']:
                self._add_credit_or_debit_entry(store, data)

        return 200

    def get(self):
        # Retrieve Till data
        with api.new_store() as store:
            till = Till.get_last(store)

            if not till:
                return None

            till_data = {
                'status': till.status,
                'opening_date': till.opening_date.strftime('%Y-%m-%d'),
                'closing_date': (till.closing_date.strftime('%Y-%m-%d') if
                                 till.closing_date else None),
                'initial_cash_amount': str(till.initial_cash_amount),
                'final_cash_amount': str(till.final_cash_amount),
                # Get payments data that will be used on 'close_till' action.
                'entry_types': till.status == 'open' and self._get_till_summary(store, till) or [],
            }

        return till_data


class ClientResource(_BaseResource):
    """Client RESTful resource."""
    routes = ['/client']

    def _dump_client(self, client):
        person = client.person
        birthdate = person.individual.birth_date if person.individual else None

        saleviews = person.client.get_client_sales().order_by(Desc('confirm_date'))
        last_items = {}
        for saleview in saleviews:
            for item in saleview.sale.get_items():
                last_items[item.sellable_id] = item.sellable.description
                # Just the last 3 products the client bought
                if len(last_items) == 3:
                    break

        if person.company:
            doc = person.company.cnpj
        else:
            doc = person.individual.cpf

        category_name = client.category.name if client.category else ""

        data = dict(
            id=client.id,
            category=client.category_id,
            doc=doc,
            last_items=last_items,
            name=person.name,
            birthdate=birthdate,
            category_name=category_name,
        )
        return data

    def _get_by_doc(self, store, data, doc):
        # Extra precaution in case we ever send the cpf already formatted
        document = format_cpf(raw_document(doc))

        person = Person.get_by_document(store, document)
        if not person or not person.client:
            return data

        return self._dump_client(person.client)

    def _get_by_category(self, store, category_name):
        tables = [Client,
                  Join(ClientCategory, Client.category_id == ClientCategory.id)]
        clients = store.using(*tables).find(Client, ClientCategory.name == category_name)
        retval = []
        for client in clients:
            retval.append(self._dump_client(client))
        return retval

    def post(self):
        data = request.get_json()

        with api.new_store() as store:
            if data.get('doc'):
                return self._get_by_doc(store, data, data['doc'])
            elif data.get('category_name'):
                return self._get_by_category(store, data['category_name'])
        return data


class LoginResource(_BaseResource):
    """Login RESTful resource."""

    routes = ['/login']

    def post(self):
        username = self.get_arg('user')
        pw_hash = self.get_arg('pw_hash')

        with api.new_store() as store:
            try:
                # FIXME: Respect the branch the user is in.
                user = LoginUser.authenticate(store, username, pw_hash, current_branch=None)
                # StoqTransactionHistory will use the current user to set the
                # responsible for the stock change
                provide_utility(ICurrentUser, user, replace=True)
            except LoginError as e:
                abort(403, str(e))

        with _get_session() as s:
            session_id = str(uuid.uuid1()).replace('-', '')
            s[session_id] = {
                'date': localnow(),
                'user_id': user.id
            }

        return session_id


class AuthResource(_BaseResource):
    """Authenticate a user agasint the database.

    This will not replace the ICurrentUser. It will just validate if a login/password is valid.
    """

    routes = ['/auth']
    method_decorators = [_login_required, _store_provider]

    def post(self, store):
        username = self.get_arg('user')
        pw_hash = self.get_arg('pw_hash')
        permission = self.get_arg('permission')

        try:
            # FIXME: Respect the branch the user is in.
            user = LoginUser.authenticate(store, username, pw_hash, current_branch=None)
        except LoginError as e:
            return make_response(str(e), 403)

        if user.profile.check_app_permission(permission):
            return True
        return make_response(_('User does not have permission'), 403)


class EventStream(_BaseResource):
    """A stream of events from this server to the application.

    Callsites can use EventStream.put(event) to send a message from the server to the client
    asynchronously.

    Note that there should be only one client connected at a time. If more than one are connected,
    all of them will receive all events
    """
    _streams = []

    routes = ['/stream']

    @classmethod
    def put(cls, data):
        # Put event in all streams
        for stream in cls._streams:
            stream.put(data)

    def _loop(self, stream):
        while True:
            data = stream.get()
            yield "data: " + json.dumps(data) + "\n\n"

    def get(self):
        stream = Queue()
        self._streams.append(stream)

        # If we dont put one event, the event stream does not seem to get stabilished in the browser
        stream.put(json.dumps({}))
        return Response(self._loop(stream), mimetype="text/event-stream")


if has_ntk:
    class TefResource(_BaseResource):
        routes = ['/tef']
        method_decorators = [_login_required]

        waiting_reply = Event()
        reply = Queue()

        NTK_MODES = {
            'credit': Ntk.TYPE_CREDIT,
            'debit': Ntk.TYPE_DEBIT,
            'voucher': Ntk.TYPE_VOUCHER,
        }

        def _print_callback(self, full, holder, merchant, short):
            printer = api.device_manager.printer
            if not printer:
                print(full)
                print(holder)
                print(merchant)
                print(short)
                return

            if (holder or short) and merchant:
                #printer.print_line(merchant)
                #printer.cut_paper()
                printer.print_line(short or holder)
                printer.cut_paper()
            elif full:
                printer.print_line(full)
                printer.cut_paper()

        def _message_callback(self, message):
            EventStream.put({
                'type': 'TEF_DISPLAY_MESSAGE',
                'message': message
            })

        def _question_callback(self, questions):
            # Right now we support asking only one question at a time. This could be imporved
            info = questions[0]
            EventStream.put({
                'type': 'TEF_ASK_QUESTION',
                'data': info.get_dict()
            })
            if info.data_type not in [PwDat.MENU, PwDat.TYPED]:
                # This is just an information for the user. No need to wait for a reply.
                return True

            self.waiting_reply.set()
            reply = self.reply.get()
            self.waiting_reply.clear()
            if not reply:
                print('cancelled')
                return False

            kwargs = {
                info.identificador.name: reply
            }
            ntk.add_params(**kwargs)
            return True

        def post(self):
            if not ntk:
                return
            try:
                self.test_printer()
            except Exception:
                EventStream.put({
                    'type': 'TEF_OPERATION_FINISHED',
                    'success': False,
                    'message': 'Erro comunicando com a impressora',
                })
                return

            data = request.get_json()
            if self.waiting_reply.is_set() and data['operation'] == 'reply':
                # There is already an operation happening, but its waiting for a user reply.
                # This is the reply
                self.reply.put(json.loads(data['value']))
                return

            ntk.set_message_callback(self._message_callback)
            ntk.set_question_callback(self._question_callback)
            ntk.set_print_callback(self._print_callback)

            try:
                # This operation will be blocked here until its complete, but since we are running
                # each request using threads, the server will still be available to handle other
                # requests (specially when handling comunication with the user through the callbacks
                # above)
                if data['operation'] == 'sale':
                    retval = ntk.sale(value=data['value'], card_type=self.NTK_MODES[data['mode']])
                elif data['operation'] == 'admin':
                    # Admin operation does not leave pending transaction
                    retval = ntk.admin()
                elif data['operation'] == 'sale_void':
                    # Admin operation does not leave pending transaction
                    retval = ntk.sale_void()
            except NtkException:
                retval = False

            message = ntk.get_info(PwInfo.RESULTMSG)
            EventStream.put({
                'type': 'TEF_OPERATION_FINISHED',
                'success': retval,
                'message': message,
            })


class ImageResource(_BaseResource):
    """Image RESTful resource."""

    routes = ['/image/<id>']

    def get(self, id):
        is_main = bool(request.args.get('is_main', None))
        # FIXME: The images should store tags so they could be requested by that tag and
        # product_id. At the moment, we simply check if the image is main or not and
        # return the first one.
        with api.new_store() as store:
            image = store.find(Image, sellable_id=id, is_main=is_main).any()

            if image:
                return send_file(io.BytesIO(image.image), mimetype='image/png')
            else:
                response = make_response(base64.b64decode(TRANSPARENT_PIXEL))
                response.headers.set('Content-Type', 'image/jpeg')
                return response


class SaleResource(_BaseResource):
    """Sellable category RESTful resource."""

    routes = ['/sale']
    method_decorators = [_login_required, _store_provider]

    PROVIDER_MAP = {
        'ELO CREDITO': 'ELO',
        'TICKET RESTA': 'TICKET REFEICAO',
        'VISA ELECTRO': 'VISA',
        'MAESTROCP': 'MASTER',
        'MASTERCARD D': 'MASTER',
        'MASTERCARD': 'MASTER',
    }

    def _get_card_device(self, store, name):
        device = store.find(CardPaymentDevice, description=name).any()
        if not device:
            device = CardPaymentDevice(store=store, description=name)
        return device

    def _get_provider(self, store, name):
        name = name.strip()
        name = self.PROVIDER_MAP.get(name, name)
        provider = store.find(CreditProvider, provider_id=name).one()
        if not provider:
            provider = CreditProvider(store=store, short_name=name, provider_id=name)
        return provider

    def post(self, store):
        self.test_printer()

        data = request.get_json()
        client_id = data.get('client_id')
        products = data['products']
        payments = data['payments']
        client_category_id = data.get('price_table')

        document = raw_document(data.get('client_document', '') or '')
        if document:
            document = format_document(document)

        if client_id:
            client = store.get(Client, client_id)
        elif document:
            person = Person.get_by_document(store, document)
            client = person and person.client
        else:
            client = None

        # Create the sale
        branch = api.get_current_branch(store)
        group = PaymentGroup(store=store)
        user = store.get(LoginUser, session['user_id'])
        sale = Sale(
            store=store,
            branch=branch,
            salesperson=user.person.sales_person,
            client=client,
            client_category_id=client_category_id,
            group=group,
            open_date=localnow(),
            coupon_id=None,
        )
        # Add products
        for p in products:
            sellable = store.get(Sellable, p['id'])
            item = sale.add_sellable(sellable, price=currency(p['price']),
                                     quantity=decimal.Decimal(p['quantity']))
            # XXX: bdil has requested that when there is a special discount, the discount does
            # not appear on the coupon. Instead, the item wil be sold using the discount price
            # as the base price. Maybe this should be a parameter somewhere
            item.base_price = item.price

        # Add payments
        sale_total = sale.get_total_sale_amount()
        for p in payments:
            method_name = p['method']
            tef_data = p.get('tef_data', {})
            if method_name == 'tef':
                p['provider'] = tef_data['card_name']
                method_name = 'card'

            method = PaymentMethod.get_by_name(store, method_name)
            installments = p.get('installments', 1) or 1

            due_dates = list(create_date_interval(
                INTERVALTYPE_MONTH,
                interval=1,
                start_date=localnow(),
                count=installments))

            # FIXME FIXME FIXME
            payment_value = currency(p['value'])
            if payment_value > sale_total:
                payment_value = sale_total

            p_list = method.create_payments(
                Payment.TYPE_IN, group, branch,
                payment_value, due_dates)

            if method.method_name == 'card':
                for payment in p_list:
                    card_data = method.operation.get_card_data_by_payment(payment)

                    card_type = p['mode']
                    # Stoq does not have the voucher comcept, so register it as a debit card.
                    if card_type == 'voucher':
                        card_type = 'debit'
                    device = self._get_card_device(store, 'TEF')
                    provider = self._get_provider(store, p['provider'])

                    if tef_data:
                        card_data.nsu = tef_data['aut_loc_ref']
                        card_data.auth = tef_data['aut_ext_ref']
                    card_data.update_card_data(device, provider, card_type, installments)
                    card_data.te.metadata = tef_data

        # Confirm the sale
        group.confirm()
        sale.order()

        till = Till.get_last(store)
        sale.confirm(till)

        # Fiscal plugins will connect to this event and "do their job"
        # It's their responsibility to raise an exception in case of
        # any error, which will then trigger the abort bellow
        SaleConfirmedRemoteEvent.emit(sale, document)

        # This will make sure we update any stock or price changes products may
        # have between sales
        return True


def bootstrap_app():
    app = Flask(__name__)
    # Indexing some session data by the USER_HASH will help to avoid maintaining
    # sessions between two different databases. This could lead to some errors in
    # the POS in which the user making the sale does not exist.
    app.config['SECRET_KEY'] = _get_user_hash()
    flask_api = Api(app)

    for cls in _BaseResource.__subclasses__():
        flask_api.add_resource(cls, *cls.routes)

    if has_ntk:
        global ntk
        config = get_config()
        if config:
            config_dir = config.get_config_directory()
            tef_dir = os.path.join(config_dir, 'ntk')
        else:
            # Tests don't have a config set. Use the plugin path as tef_dir, since it also has the
            # library
            import stoqntk
            tef_dir = os.path.dirname(os.path.dirname(stoqntk.__file__))

        ntk = Ntk()
        ntk.init(tef_dir)

    return app


def run_flaskserver(port, debug=False):
    from stoqlib.lib.environment import configure_locale
    # Force pt_BR for now.
    configure_locale('pt_BR')

    # Check drawer in a separated thread
    for function in WORKERS:
        threadit(function)

    app = bootstrap_app()
    app.debug = debug

    @app.after_request
    def after_request(response):
        # Add all the CORS headers the POS needs to have its ajax requests
        # accepted by the browser
        origin = request.headers.get('origin')
        if not origin:
            origin = request.args.get('origin', request.form.get('origin', '*'))
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Methods'] = 'POST, GET, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'stoq-session, Content-Type'
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        return response

    @app.errorhandler(Exception)
    def unhandled_exception(e):
        log.exception('Unhandled Exception: %s', (e))
        return 'bad request!', 500

    app.run('0.0.0.0', port=port, debug=debug, threaded=True)
