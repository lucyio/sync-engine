import sys
import time
from contextlib import contextmanager

from sqlalchemy import event
from sqlalchemy.orm.session import Session
from sqlalchemy.exc import OperationalError

from inbox.config import config
from inbox.ignition import main_engine
from inbox.util.stats import statsd_client
from inbox.log import get_logger, _find_first_app_frame_and_name
log = get_logger()


cached_engine = None


def new_session(engine, versioned=True):
    """Returns a session bound to the given engine."""
    session = Session(bind=engine, autoflush=True, autocommit=False)
    if versioned:
        from inbox.models.transaction import (create_revisions,
                                              propagate_changes,
                                              increment_versions)

        @event.listens_for(session, 'before_flush')
        def before_flush(session, flush_context, instances):
            propagate_changes(session)
            increment_versions(session)

        @event.listens_for(session, 'after_flush')
        def after_flush(session, flush_context):
            """
            Hook to log revision snapshots. Must be post-flush in order to
            grab object IDs on new objects.

            """
            create_revisions(session)

        # Make statsd calls for transaction times
        transaction_start_map = {}
        frame, modname = _find_first_app_frame_and_name(
                ignores=['sqlalchemy', 'inbox.models.session', 'inbox.log',
                         'contextlib'])
        funcname = frame.f_code.co_name
        modname = modname.replace(".", "-")
        metric_name = 'db.{}.{}'.format(modname, funcname)

        @event.listens_for(session, 'after_transaction_create')
        def after_transaction_create(session, transaction):
            transaction_start_map[hash(transaction)] = time.time()

        @event.listens_for(session, 'after_transaction_end')
        def after_transaction_end(session, transaction):
            start_time = transaction_start_map.get(hash(transaction))
            if not start_time:
                return

            latency = int((time.time() - start_time) * 1000)
            statsd_client.timing(metric_name, latency)
            statsd_client.incr(metric_name)

    return session

# Old name for legacy code.
InboxSession = new_session


@contextmanager
def session_scope(versioned=True, debug=False):
    """ Provide a transactional scope around a series of operations.

    Takes care of rolling back failed transactions and closing the session
    when it goes out of scope.

    Note that sqlalchemy automatically starts a new database transaction when
    the session is created, and restarts a new transaction after every commit()
    on the session. Your database backend's transaction semantics are important
    here when reasoning about concurrency.

    Parameters
    ----------
    versioned : bool
        Do you want to enable the transaction log?
    debug : bool
        Do you want to turn on SQL echoing? Use with caution. Engine is not
        cached in this case!

    Yields
    ------
    Session
        The created session.
    """

    global cached_engine
    if cached_engine is None and not debug:
        cached_engine = main_engine()
        log.info("Don't yet have engine... creating default from ignition",
                 engine=id(cached_engine))

    if debug:
        session = new_session(main_engine(echo=True), versioned)
    else:
        session = new_session(cached_engine, versioned)

    try:
        if config.get('LOG_DB_SESSIONS'):
            start_time = time.time()
            calling_frame = sys._getframe().f_back.f_back
            call_loc = '{}:{}'.format(calling_frame.f_globals.get('__name__'),
                                      calling_frame.f_lineno)
            logger = log.bind(engine_id=id(cached_engine),
                              session_id=id(session), call_loc=call_loc)
            logger.info('creating db_session',
                        sessions_used=cached_engine.pool.checkedout())
        yield session
        session.commit()
    except BaseException as exc:
        try:
            session.rollback()
            raise
        except OperationalError:
            log.warn('Encountered OperationalError on rollback',
                     original_exception=type(exc))
            raise exc
    finally:
        if config.get('LOG_DB_SESSIONS'):
            lifetime = time.time() - start_time
            logger.info('closing db_session', lifetime=lifetime,
                        sessions_used=cached_engine.pool.checkedout())
        session.close()
