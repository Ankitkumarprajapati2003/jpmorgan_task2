from random import normalvariate, random
from datetime import timedelta, datetime
import csv
import dateutil.parser
import os.path
import operator
import json
import re
import threading
import http.server
from socketserver import ThreadingMixIn

################################################################################
#
# Config

REALTIME = True
SIM_LENGTH = timedelta(days=365 * 5)
MARKET_OPEN = datetime.today().replace(hour=0, minute=30, second=0)

# Market parameters
SPD = (2.0, 6.0, 0.1)  # Spread parameters (min, max, std)
PX = (60.0, 150.0, 1)   # Price parameters (min, max, std)
FREQ = (12, 36, 50)     # Frequency parameters (min, max, std)

# Trades
OVERLAP = 4

################################################################################
#
# Test Data

def bwalk(min, max, std):
    """ Generates a bounded random walk. """
    rng = max - min
    while True:
        max += normalvariate(0, std)
        yield abs((max % (rng * 2)) - rng) + min

def market(t0=MARKET_OPEN):
    """ Generates a random series of market conditions (time, price, spread). """
    for hours, px, spd in zip(bwalk(*FREQ), bwalk(*PX), bwalk(*SPD)):
        yield t0, px, spd
        t0 += timedelta(hours=abs(hours))

def orders(hist):
    """ Generates a random set of limit orders (time, stock, side, price, size). """
    for t, px, spd in hist:
        stock = 'ABC' if random() > 0.5 else 'DEF'
        side, d = ('sell', 2) if random() > 0.5 else ('buy', -2)
        order = round(normalvariate(px + (spd / d), spd / OVERLAP), 2)
        size = int(abs(normalvariate(0, 100)))
        yield t, stock, side, order, size

################################################################################
#
# Order Book

def add_book(book, order, size, _age=10):
    """ Adds a new order and size to a book, and ages the rest of the book. """
    yield order, size, _age
    for o, s, age in book:
        if age > 0:
            yield o, s, age - 1

def clear_order(order, size, book, op=operator.ge, _notional=0):
    """ Tries to clear a sized order against a book, returning a tuple of
        (notional, new_book) if successful, and None if not.
    """
    (top_order, top_size, age), tail = book[0], book[1:]
    if op(order, top_order):
        _notional += min(size, top_size) * top_order
        sdiff = top_size - size
        if sdiff > 0:
            return _notional, list(add_book(tail, top_order, sdiff, age))
        elif len(tail) > 0:
            return clear_order(order, -sdiff, tail, op, _notional)

def clear_book(buy=None, sell=None):
    """ Clears all crossed orders from a buy and sell book, returning the new
        books uncrossed.
    """
    while buy and sell:
        order, size, _ = buy[0]
        new_book = clear_order(order, size, sell)
        if new_book:
            sell = new_book[1]
            buy = buy[1:]
        else:
            break
    return buy, sell

def order_book(orders, book, stock_name):
    """ Generates a series of order books from a series of orders. """
    for t, stock, side, order, size in orders:
        if stock_name == stock:
            new = add_book(book.get(side, []), order, size)
            book[side] = sorted(new, reverse=side == 'buy', key=lambda x: x[0])
        bids, asks = clear_book(**book)
        yield t, bids, asks

################################################################################
#
# Test Data Persistence

def generate_csv():
    """ Generates a CSV of order history. """
    with open('test.csv', 'w', newline='') as f:
        writer = csv.writer(f)
        for t, stock, side, order, size in orders(market()):
            if t > MARKET_OPEN + SIM_LENGTH:
                break
            writer.writerow([t, stock, side, order, size])

def read_csv():
    """ Reads a CSV of order history into a list. """
    with open('test.csv', 'r', newline='') as f:
        for time, stock, side, order, size in csv.reader(f):
            yield dateutil.parser.parse(time), stock, side, float(order), int(size)

################################################################################
#
# Server

class ThreadedHTTPServer(ThreadingMixIn, http.server.HTTPServer):
    """ Multithreaded HTTP Server class with proper shutdown. """
    allow_reuse_address = True

    def shutdown(self):
        """ Proper shutdown of the server. """
        self.socket.close()
        http.server.HTTPServer.shutdown(self)

def route(path):
    """ Decorator for routing paths to methods. """
    def _route(f):
        setattr(f, '__route__', path)
        return f
    return _route

def read_params(path):
    """ Reads query parameters into a dictionary. """
    query = path.split('?')
    if len(query) > 1:
        query = query[1].split('&')
        return dict(map(lambda x: x.split('='), query))

def get(req_handler, routes):
    """ Maps a request to the appropriate route. """
    for name, handler in routes.__class__.__dict__.items():
        if hasattr(handler, "__route__"):
            if None != re.search(handler.__route__, req_handler.path):
                req_handler.send_response(200)
                req_handler.send_header('Content-Type', 'application/json')
                req_handler.send_header('Access-Control-Allow-Origin', '*')
                req_handler.end_headers()
                params = read_params(req_handler.path)
                data = json.dumps(handler(routes, params)) + '\n'
                req_handler.wfile.write(bytes(data, encoding='utf-8'))
                return

def run(routes, host='0.0.0.0', port=8080):
    """ Runs the server with threaded HTTP handling. """
    class RequestHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args, **kwargs):
            pass

        def do_GET(self):
            get(self, routes)

    server = ThreadedHTTPServer((host, port), RequestHandler)
    try:
        print(f"HTTP server started on port {port}")
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down the server...")
        server.shutdown()
        server.server_close()

################################################################################
#
# App

ops = {
    'buy': operator.le,
    'sell': operator.ge,
}

class App(object):
    """ Main application class for the trading game server. """

    def __init__(self):
        self._book_1 = dict()
        self._book_2 = dict()
        self._data_1 = None
        self._data_2 = None
        self._rt_start = datetime.now()
        self._sim_start = None
        self.initialize_data_feeds()

    def initialize_data_feeds(self):
        """ Initializes data feeds and handles errors. """
        try:
            self._data_1 = order_book(read_csv(), self._book_1, 'ABC')
            self._data_2 = order_book(read_csv(), self._book_2, 'DEF')
            self._sim_start, _, _ = next(self._data_1)
            self.read_10_first_lines()
        except Exception as e:
            print(f"Error initializing data feeds: {e}")
            # Optionally handle initialization errors here

    @property
    def _current_book_1(self):
        """ Generator for current book 1. """
        for t, bids, asks in self._data_1:
            if REALTIME:
                while t > self._sim_start + (datetime.now() - self._rt_start):
                    yield t, bids, asks
            else:
                yield t, bids, asks

    @property
    def _current_book_2(self):
        """ Generator for current book 2. """
        for t, bids, asks in self._data_2:
            if REALTIME:
                while t > self._sim_start + (datetime.now() - self._rt_start):
                    yield t, bids, asks
            else:
                yield t, bids, asks

    def read_10_first_lines(self):
        """ Reads the first 10 lines from data feeds. """
        for _ in range(10):
            next(self._data_1)
            next(self._data_2)

    # @route('/query')
    # def handle_query(self, x):
    #     """ Handles query requests. """
    #     try:
    #         t1, bids1, asks1 = next(self._current_book_1)
    #         t2, bids2, asks2 = next(self._current_book_2)
    #     except StopIteration:
    #         print("Data feed exhausted, reinitializing...")
    #         self.initialize_data_feeds()
    #         t1, bids1, asks1 = next(self._current_book_1)
    #         t2, bids2, asks2 = next(self._current_book_2)

    #     t = max(t1, t2) if t1 and t2 else t1 or t2
    #     print(f'Query received @ t {t}')

    #     return [
    #         {
    #             'id': x and x.get('id', None),
    #             'stock': 'ABC',
    #             'timestamp': str(t),
    #             'top_bid': bids1 and {'price': bids1[0][0], 'size': bids1[0][1]},
    #             'top_ask': asks1 and {'price': asks1[0][0], 'size': asks1[0][1]}
    #         },
    #         {
    #             'id': x and x.get('id', None),
    #             'stock': 'DEF',
    #             'timestamp': str(t),
    #             'top_bid': bids2 and {'price': bids2[0][0], 'size': bids2[0][1]},
    #             'top_ask': asks2 and {'price': asks2[0][0], 'size': asks2[0][1]}
    #         }
    #     ]

    @route('/query')
    def handle_query(self, x):
        """ Handles query requests. """
        try:
            t1, bids1, asks1 = next(self._current_book_1)
            t2, bids2, asks2 = next(self._current_book_2)
        except StopIteration:
            print("Data feed exhausted, reinitializing...")
            self.initialize_data_feeds()  # Reinitialize data feeds
            t1, bids1, asks1 = next(self._current_book_1)
            t2, bids2, asks2 = next(self._current_book_2)

        t = max(t1, t2) if t1 and t2 else t1 or t2
        print(f'Query received @ t {t}')

    
        return [
            {
                'id': x and x.get('id', None),
                'stock': 'ABC',
                'timestamp': str(t),
                'top_bid': bids1 and {'price': bids1[0][0], 'size': bids1[0][1]},
                'top_ask': asks1 and {'price': asks1[0][0], 'size': asks1[0][1]}
            },
            {
                'id': x and x.get('id', None),
                'stock': 'DEF',
                'timestamp': str(t),
                'top_bid': bids2 and {'price': bids2[0][0], 'size': bids2[0][1]},
                'top_ask': asks2 and {'price': asks2[0][0], 'size': asks2[0][1]}
            }
        ]

################################################################################
#
# Main

if __name__ == '__main__':
    if not os.path.isfile('test.csv'):
        print("No data found, generating...")
        generate_csv()
    run(App())
