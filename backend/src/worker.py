import os,time,json,logging,uuid
from datetime import timedelta
from pathlib import Path
import redis
from sqlalchemy import select
from .db import Session
from .models import Pack,AnalysisRun,utcnow
from .basket_rules import recommend

q=redis.Redis.from_url(os.environ['REDIS_URL'],decode_responses=True)
QUEUE='book-baskets'
LEASE=timedelta(seconds=int(os.getenv('ANALYSIS_LEASE_SECONDS','120')))
RECOVER_ON_START=os.getenv('ANALYSIS_RECOVER_ON_START','true').lower()=='true'
ARTIFACTS=Path(os.getenv('ARTIFACTS_DIR','/artifacts'))


def enqueue(run_id):
    q.rpush(QUEUE,run_id)


def _pack_id(s,run_id):
    row=s.execute(select(AnalysisRun.pack_id).where(AnalysisRun.id==run_id)).first()
    return row[0]if row else None


def claim(s,run_id):
    """Atomically move queued -> running for this exact run.

    A stale queue item (abandoned run, superseded run, or a duplicate delivery
    after a successful publish) is skipped instead of recomputed, so one attempt
    cannot emit a second recommendation. Pack is locked before Run everywhere.
    Each claim gets a fresh lease token; only the holder of the current token may
    publish, so a slow attempt whose lease already expired and was re-queued
    cannot overwrite the retried result.
    """
    pack_id=_pack_id(s,run_id)
    if pack_id is None:return None
    p=s.scalar(select(Pack).where(Pack.id==pack_id).with_for_update().execution_options(populate_existing=True))
    run=s.scalar(select(AnalysisRun).where(AnalysisRun.id==run_id).with_for_update().execution_options(populate_existing=True))
    if not run or run.state!='queued':return None
    if p and p.active_run_id!=run.id:
        run.state='superseded';run.finished_at=utcnow()
        s.flush()
        return None
    run.state='running';run.started_at=utcnow();run.lease_expires_at=utcnow()+LEASE;run.lease_token=uuid.uuid4().hex
    if p:p.ml_state='running'
    s.flush()
    return run


def compute(s,run):
    # Resume always uses the snapshots frozen when the run was first opened:
    # the original historical basket batch and the pack subjects at that time.
    baskets=[b['subjects']for b in run.basket_snapshot]
    subjects=[e['subject']for e in run.edition_snapshot]
    return recommend(baskets,subjects)


def publish(s,run_id,result,token=None):
    """Publish a result only if its run is still the pack's active attempt.

    The state check and the write both happen under the Pack row lock. If the
    operator abandoned this attempt (or opened a newer one) while it was
    computing, the newer active_run_id wins: this stale result is archived on its
    own run row and never overwrites the pack's current recommendation. A stale
    lease token means this attempt was already re-claimed by recovery; that
    worker also must not publish.
    """
    pack_id=_pack_id(s,run_id)
    if pack_id is None:return 'ignored'
    p=s.scalar(select(Pack).where(Pack.id==pack_id).with_for_update().execution_options(populate_existing=True))
    run=s.scalar(select(AnalysisRun).where(AnalysisRun.id==run_id).with_for_update().execution_options(populate_existing=True))
    if not run or run.state!='running':return 'ignored'
    if token is not None and run.lease_token!=token:return 'ignored'
    if not p or p.active_run_id!=run.id:
        run.state='superseded';run.recommendation=result;run.finished_at=utcnow();run.lease_expires_at=None;run.lease_token=None
        s.flush()
        return 'superseded'
    run.state='ready';run.recommendation=result;run.finished_at=utcnow();run.error=None;run.lease_expires_at=None;run.lease_token=None
    p.recommendation=result;p.ml_state='ready';p.error=None
    s.flush()
    ARTIFACTS.mkdir(parents=True,exist_ok=True)
    ARTIFACTS.joinpath(f'basket-{p.id}.json').write_text(json.dumps(result,ensure_ascii=False))
    ARTIFACTS.joinpath(f'basket-{p.id}-run-{run.id}.json').write_text(json.dumps(result,ensure_ascii=False))
    return 'ready'


def fail(s,run_id,error,token=None):
    pack_id=_pack_id(s,run_id)
    if pack_id is None:return 'ignored'
    p=s.scalar(select(Pack).where(Pack.id==pack_id).with_for_update().execution_options(populate_existing=True))
    run=s.scalar(select(AnalysisRun).where(AnalysisRun.id==run_id).with_for_update().execution_options(populate_existing=True))
    if not run or run.state!='running':return 'ignored'
    if token is not None and run.lease_token!=token:return 'ignored'
    run.state='failed';run.error=str(error)[:450];run.finished_at=utcnow();run.lease_expires_at=None;run.lease_token=None
    if p and p.active_run_id==run.id:
        p.ml_state='failed';p.error=str(error)[:450]
    s.flush()
    return 'failed'


def handle(run_id):
    with Session()as s:
        run=claim(s,run_id)
        if not run:s.commit();return 'skipped'
        token=run.lease_token
        s.commit()
        try:
            result=compute(s,run)
        except Exception as ex:
            fail(s,run_id,ex,token);s.commit();logging.exception('basket failed')
            return 'failed'
        outcome=publish(s,run_id,result,token);s.commit()
        return outcome


def recover(startup=False):
    """Resume interrupted work from the database, which is the source of truth.

    A fresh worker process cannot legitimately inherit a `running` attempt, so on
    startup every interrupted run returns to `queued` and is recomputed from its
    frozen snapshot (same basket batch and pack subjects as the first try). While
    running, only attempts whose lease has expired are recovered, so a long
    computation is not duplicated. Queued runs missing from the queue (e.g. the
    push failed at request time) are re-enqueued; duplicates are harmless because
    claim() accepts a queued run exactly once and each claim rotates the token.
    """
    with Session()as s:
        now=utcnow()
        runs=list(s.scalars(select(AnalysisRun).where(AnalysisRun.state.in_(('queued','running'))).order_by(AnalysisRun.id).with_for_update()))
        queued_runs=[];stale_runs=[]
        for run in runs:
            if run.state=='running':
                stale=run.lease_expires_at is None or run.lease_expires_at<now
                if startup or stale:
                    run.state='queued';run.started_at=None;run.lease_expires_at=None;run.lease_token=None;stale_runs.append(run.id)
            else:
                queued_runs.append(run.id)
        s.commit()
    in_queue=set(q.lrange(QUEUE,0,-1)) if queued_runs else set()
    ids=stale_runs+[r for r in queued_runs if str(r)not in in_queue]
    for run_id in ids:
        try:enqueue(run_id)
        except Exception:logging.exception('requeue failed')
    return ids


def main():
    startup_pending=RECOVER_ON_START
    if startup_pending:
        try:recover(startup=True);startup_pending=False
        except Exception:logging.exception('startup recovery deferred')
    while True:
        Path('/tmp/books-heartbeat').touch()
        try:
            item=q.blpop(QUEUE,timeout=2)
            if item:handle(int(item[1]));continue
            # retry the full startup sweep until the DB was reachable once, so an
            # attempt interrupted by the original crash is resumed promptly
            recover(startup=startup_pending);startup_pending=False
        except Exception:logging.exception('queue unavailable');time.sleep(1)


if __name__=='__main__':
    main()
