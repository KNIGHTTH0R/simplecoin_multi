from itsdangerous import TimedSerializer
from flask import current_app, request, abort

from .models import Transaction, Payout, TransactionSummary, TradeRequest
from .utils import Benchmark
from .views import main
from . import db


@main.route("/get_sell_requests", methods=['POST'])
def get_sell_requests():
    """ Used by remote procedure call to retrieve a list of sell requests to
    be processed. Transaction information is signed for safety. """
    s = TimedSerializer(current_app.config['rpc_signature'])
    args = s.loads(request.data)
    current_app.logger.info("get_sell_requests being called, args of {}!".format(args))
    lock = False
    if isinstance(args, dict) and args['lock']:
        lock = True

    sell_requests = TradeRequest.query.filter_by(_status=0, locked=False).all()
    srs = [(sr.id, sr.currency, sr.quantity) for sr in sell_requests]

    if lock:
        current_app.logger.info("Locking sell requests at retriever request.")
        for sr in sell_requests:
            sr.locked = True
        db.session.commit()
    return s.dumps([srs, lock])


@main.route("/update_sell_requests", methods=['POST'])
def update_sell_requests():
    """ Used as a response from an rpc sell request system. This will update the amount received for
    a sell request and its status. Both request and response are signed. """
    s = TimedSerializer(current_app.config['rpc_signature'])
    data = s.loads(request.data)

    # basic checking of input
    try:
        assert 'completed_srs' in data
        assert isinstance(data['completed_srs'], dict)
        for sr_id, quantity in data['completed_srs'].iteritems():
            assert isinstance(sr_id, int)
            assert isinstance(quantity, int)
    except AssertionError:
        current_app.logger.warn("Invalid data passed to update_sell_requests", exc_info=True)
        abort(400)

    if data['update'] and data['completed_srs']:
        for ts_id, quantity in data['completed_srs'].iteritems():
            ts = TradeRequest.query.filter(TradeRequest).filter_by(id=ts_id).first()
            ts.exchanged_quantity = quantity
            ts._status = 4
        db.session.commit()
        return s.dumps(dict(success=True, result="Sell requests successfully updated."))

    return s.dumps(True)


@main.route("/reset_sell_requests", methods=['POST'])
def reset_sell_requests():
    """ Used as a response from an rpc sell request system. This will reset
    the locked status of a list of sell requests upon failure on the remote
    side. Both request and response are signed. """
    s = TimedSerializer(current_app.config['rpc_signature'])
    data = s.loads(request.data)

    # basic checking of input
    try:
        assert 'reset' in data
        assert isinstance(data['reset'], bool)
        assert isinstance(data['sr_ids'], list)
        for id in data['sr_ids']:
            assert isinstance(id, int)
    except AssertionError:
        current_app.logger.warn("Invalid data passed to reset_sell_requests", exc_info=True)
        abort(400)

    if data['reset'] and data['sr_ids']:
        srs = TradeRequest.query.filter(TradeRequest.id.in_(data['sr_ids'])).all()
        for sr in srs:
            sr.locked = False
        db.session.commit()
        return s.dumps(dict(success=True, result="Successfully reset"))

    return s.dumps(True)


@main.route("/get_final_payouts", methods=['GET'])
def get_final_payouts():
    """ Used by remote procedure call to retrieve a list of final payouts to
    be processed. Information is signed for safety. """
    s = TimedSerializer(current_app.config['rpc_signature'])
    args = s.loads(request.data)
    current_app.logger.info("get_final_payouts being called, args of {}!".format(args))

    final_payouts = []

    fps = [(fp.id, fp.quantity, fp.desired_currency) for fp in final_payouts]

    return s.dumps([fps])


@main.route("/get_payouts", methods=['POST'])
def get_payouts():
    """ Used by remote procedure call to retrieve a list of transactions to
    be processed. Transaction information is signed for safety. """
    s = TimedSerializer(current_app.config['rpc_signature'])
    args = s.loads(request.data)
    current_app.logger.info("get_payouts being called, args of {}!".format(args))
    lock = False
    merged = None
    if isinstance(args, dict) and args['lock']:
        lock = True
    if isinstance(args, dict) and args['merged']:
        merged = args['merged']

    with Benchmark("Fetching payout information"):
        pids = [(p.user, p.amount, p.id) for p in Payout.query.filter_by(transaction_id=None, locked=False, merged_type=merged).
                join(Payout.block, aliased=True).filter_by(mature=True)]

        if lock:
            if pids:
                current_app.logger.info("Locking {} payout ids at retriever request."
                                        .format(len(pids)))
                (Payout.query.filter(Payout.id.in_(p[2] for p in pids))
                 .update({Payout.locked: True}, synchronize_session=False))
            db.session.commit()
    return s.dumps([pids, lock])


@main.route("/update_payouts", methods=['POST'])
def update_transactions():
    """ Used as a response from an rpc payout system. This will either reset
    the locked status of a list of transactions upon failure on the remote
    side, or create a new CoinTransaction object and link it to the
    transactions to signify that the transaction has been processed. Both
    request and response are signed. """
    s = TimedSerializer(current_app.config['rpc_signature'])
    data = s.loads(request.data)

    # basic checking of input
    try:
        if 'coin_txid' in data:
            assert len(data['coin_txid']) == 64
        else:
            assert 'reset' in data
            assert isinstance(data['reset'], bool)
        assert isinstance(data['pids'], list)
        assert isinstance(data['bids'], list)
        for id in data['pids']:
            assert isinstance(id, int)
        for id in data['bids']:
            assert isinstance(id, int)
    except AssertionError:
        current_app.logger.warn("Invalid data passed to confirm", exc_info=True)
        abort(400)

    if 'coin_txid' in data:
        with Benchmark("Associating payout transaction ids"):
            merged_type = data.get('merged', None)
            coin_trans = Transaction.create(data['coin_txid'], merged_type=merged_type)
            db.session.flush()
            user_amounts = {}
            user_counts = {}
            for payout in Payout.query.filter(Payout.id.in_(data['pids'])):
                user_counts.setdefault(payout.user, 0)
                user_amounts.setdefault(payout.user, 0)
                user_amounts[payout.user] += payout.amount
                user_counts[payout.user] += 1

            for user in user_counts:
                TransactionSummary.create(
                    coin_trans.txid, user, user_amounts[user], user_counts[user])

            if data['pids']:
                Payout.query.filter(Payout.id.in_(data['pids'])).update(
                    {Payout.transaction_id: coin_trans.txid}, synchronize_session=False)

            db.session.commit()
    elif data['reset']:
        with Benchmark("Resetting {:,} payouts and {:,} bonus payouts locked status"
                       .format(len(data['pids']), len(data['bids']))):
            if data['pids']:
                Payout.query.filter(Payout.id.in_(data['pids'])).update(
                    {Payout.locked: False}, synchronize_session=False)
            db.session.commit()
        return s.dumps(dict(success=True, result="Successfully reset"))

    return s.dumps(True)


@main.route("/confirm_transactions", methods=['POST'])
def confirm_transactions():
    """ Used to confirm that a transaction is now complete on the network. """
    s = TimedSerializer(current_app.config['rpc_signature'])
    data = s.loads(request.data)

    # basic checking of input
    try:
        assert isinstance(data['tids'], list)
    except AssertionError:
        current_app.logger.warn("Invalid data passed to confirm_transactions",
                                exc_info=True)
        abort(400)

    Transaction.query.filter(Transaction.txid.in_(data['tids'])).update(
        {Transaction.confirmed: True}, synchronize_session=False)
    db.session.commit()

    return s.dumps(True)
