import pytest
from sqlalchemy import select

import conftest  # noqa: F401  (sets env before app imports)
import fakeredis

from src.db import Base,engine,Session
from src import seed_service,worker
from src.models import Pack,PackLine,AnalysisRun,HistoricalBasket,Edition,Classroom
from src import analysis_service
from src.basket_rules import recommend


class User:
    email='operator@xunshu.demo'
    role='operator'


class Reason:
    def __init__(self,reason=''):self.reason=reason


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    fake=fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(worker,'q',fake)
    seed_service.seed()
    yield fake


def make_pack(s,code='PACK-2026-G13'):
    room=s.scalar(select(Classroom).where(Classroom.code=='G1-3'))
    phy=s.scalar(select(Edition).where(Edition.code=='PHY-V2'))
    lab=s.scalar(select(Edition).where(Edition.code=='LAB-V2'))
    p=Pack(code=code,classroom_id=room.id,term='2026秋')
    s.add(p);s.flush()
    s.add_all([PackLine(pack_id=p.id,edition_id=phy.id,quantity=12),PackLine(pack_id=p.id,edition_id=lab.id,quantity=12)])
    s.commit()
    return p.id


def run_count():
    with Session()as s:return len(list(s.scalars(select(AnalysisRun))))


def test_retry_after_interruption_reuses_original_snapshot():
    with Session()as s:
        pack_id=make_pack(s)
        p,run,mode=analysis_service.start(s,pack_id,User());s.commit()
        run_id=run.id
        first_editions=run.edition_snapshot
        first_baskets=run.basket_snapshot
        # worker claimed it, then the service died mid-computation
        worker.claim(s,run_id);s.commit()
        # sample set and pack lines change after the run was opened
        s.add(HistoricalBasket(code='LATE-ONLY',subjects=['art']))
        s.scalar(select(PackLine).where(PackLine.pack_id==pack_id)).edition_id=s.scalar(select(Edition.id).where(Edition.code=='MATH-V2'))
        s.commit()
    # service restored: worker recovers the interrupted attempt from the DB
    assert worker.recover(startup=True)==[run_id]
    with Session()as s:
        run=s.get(AnalysisRun,run_id)
        assert run.state=='queued'
        assert run.edition_snapshot==first_editions
        assert run.basket_snapshot==first_baskets
        assert 'LATE-ONLY'not in [b['code']for b in run.basket_snapshot]
    assert worker.handle(run_id)=='ready'
    with Session()as s:
        run=s.get(AnalysisRun,run_id)
        assert run.state=='ready'and run.recommendation['train_rows']==72
        assert run.recommendation['source_checksum']==recommend([b['subjects']for b in first_baskets],[e['subject']for e in first_editions])['source_checksum']
        p=s.get(Pack,pack_id)
        assert p.ml_state=='ready'and p.recommendation==run.recommendation
    assert run_count()==1


def test_abandon_then_new_run_uses_new_sample_and_supersedes_old():
    with Session()as s:
        pack_id=make_pack(s)
        _,old,_=analysis_service.start(s,pack_id,User());s.commit()
        old_id=old.id
    # the first attempt is stuck in running; the manager gives up on it
    with Session()as s:
        assert worker.claim(s,old_id)is not None
        s.commit()
    with Session()as s:
        s.add(HistoricalBasket(code='NEW-SAMPLE',subjects=['art']))
        s.scalar(select(PackLine).where(PackLine.pack_id==pack_id)).edition_id=s.scalar(select(Edition.id).where(Edition.code=='MATH-V2'))
        s.commit()
    with Session()as s:
        p,abandoned=analysis_service.abandon(s,pack_id,Reason('任务卡住'),User());s.commit()
        assert abandoned.state=='abandoned'
        _,new,_=analysis_service.start(s,pack_id,User());s.commit()
        new_id=new.id
        assert 'NEW-SAMPLE'in[b['code']for b in new.basket_snapshot]
        assert new.edition_snapshot[0]['code']=='MATH-V2'
        assert p.active_run_id==new_id
    # the abandoned attempt's queued item must not resurrect or overwrite anything
    with Session()as s:assert worker.claim(s,old_id)is None
    # new run publishes
    assert worker.handle(new_id)=='ready'
    with Session()as s:
        p=s.get(Pack,pack_id)
        assert p.active_run_id==new_id and p.recommendation==s.get(AnalysisRun,new_id).recommendation
        assert s.get(AnalysisRun,old_id).state=='abandoned'


def test_stale_finish_cannot_overwrite_newer_suggestion():
    with Session()as s:
        pack_id=make_pack(s)
        _,old,_=analysis_service.start(s,pack_id,User());s.commit()
        old_id=old.id
    # old attempt starts computing
    with Session()as s:worker.claim(s,old_id);s.commit()
    # library manager gives up and opens a new analysis while old still computes
    with Session()as s:
        analysis_service.abandon(s,pack_id,Reason('太慢'),User());s.commit()
        _,new,_=analysis_service.start(s,pack_id,User());s.commit()
        new_id=new.id
    assert worker.handle(new_id)=='ready'
    with Session()as s:new_result=s.get(Pack,pack_id).recommendation
    # old attempt finally finishes: its run was abandoned, so it is ignored
    # old attempt finally finishes: its run was abandoned, so it is ignored
    with Session()as s:
        assert worker.publish(s,old_id,{'suggestions':[{'stale':True}]})=='ignored'
        s.commit()
    with Session()as s:
        p=s.get(Pack,pack_id)
        assert p.active_run_id==new_id and p.recommendation==new_result
        assert s.get(AnalysisRun,old_id).state=='abandoned'


def test_superseded_guard_archives_without_overwriting():
    with Session()as s:
        pack_id=make_pack(s)
        _,old,_=analysis_service.start(s,pack_id,User());s.commit()
        old_id=old.id
    # simulate the ordering the guard exists for: an attempt is still marked
    # running while the pack already points at a newer active attempt
    with Session()as s:worker.claim(s,old_id);s.commit()
    with Session()as s:
        analysis_service.abandon(s,pack_id,Reason('太慢'),User());s.commit()
        _,new,_=analysis_service.start(s,pack_id,User());s.commit()
        new_id=new.id
    assert worker.handle(new_id)=='ready'
    with Session()as s:new_result=s.get(Pack,pack_id).recommendation
    with Session()as s:
        s.get(AnalysisRun,old_id).state='running'
        old_copy={'suggestions':[{'stale':True}],'input_subjects':['physics']}
        s.commit()
    with Session()as s:
        assert worker.publish(s,old_id,old_copy)=='superseded'
        s.commit()
    with Session()as s:
        p=s.get(Pack,pack_id)
        assert p.active_run_id==new_id and p.recommendation==new_result
        old=s.get(AnalysisRun,old_id)
        assert old.state=='superseded'and old.recommendation==old_copy


def test_same_attempt_retry_does_not_emit_twice():
    with Session()as s:
        pack_id=make_pack(s)
        _,run,_=analysis_service.start(s,pack_id,User());s.commit()
        run_id=run.id
    with Session()as s:
        # second request while queued is refused
        with pytest.raises(Exception):analysis_service.start(s,pack_id,User())
    assert worker.handle(run_id)=='ready'
    # duplicate delivery after publish is a no-op
    assert worker.handle(run_id)=='skipped'
    assert worker.handle(run_id)=='skipped'
    with Session()as s:
        assert len(list(s.scalars(select(AnalysisRun))))==1
        assert s.get(AnalysisRun,run_id).state=='ready'
        # a ready run cannot be restarted either
        with pytest.raises(Exception):analysis_service.start(s,pack_id,User())


def test_expired_lease_retry_cannot_be_overwritten_by_old_worker():
    with Session()as s:
        pack_id=make_pack(s)
        _,run,_=analysis_service.start(s,pack_id,User());s.commit()
        run_id=run.id
    # a worker claims and is then deemed dead (lease expired, recovered)
    with Session()as s:
        first=worker.claim(s,run_id);s.commit()
        first_token=first.lease_token
        first.lease_expires_at=worker.utcnow()-worker.timedelta(seconds=1)
        s.commit()
    assert worker.recover()==[run_id]
    # the retried work completes and publishes
    with Session()as s:
        retry=worker.claim(s,run_id);s.commit()
        retry_token=retry.lease_token
    assert retry_token!=first_token
    with Session()as s:
        assert worker.publish(s,run_id,{'suggestions':[{'winner':True}]},retry_token)=='ready'
        s.commit()
    # the old worker wakes up and tries to publish its own result
    with Session()as s:
        assert worker.publish(s,run_id,{'suggestions':[{'loser':True}]},first_token)=='ignored'
        s.commit()
    with Session()as s:
        assert s.get(Pack,pack_id).recommendation=={'suggestions':[{'winner':True}]}
        assert s.get(AnalysisRun,run_id).recommendation=={'suggestions':[{'winner':True}]}


def test_failed_run_retries_same_run_not_new_attempt():
    with Session()as s:
        pack_id=make_pack(s)
        _,run,_=analysis_service.start(s,pack_id,User());s.commit()
        run_id=run.id
        snapshot=run.basket_snapshot
    with Session()as s:
        assert worker.claim(s,run_id)is not None
        s.commit()
    with Session()as s:worker.fail(s,run_id,RuntimeError('boom'));s.commit()
    with Session()as s:
        s.add(HistoricalBasket(code='AFTER-FAIL',subjects=['music']));s.commit()
    with Session()as s:
        _,same,mode=analysis_service.start(s,pack_id,User());s.commit()
        assert mode=='retry'and same.id==run_id
        assert same.basket_snapshot==snapshot
        assert 'AFTER-FAIL'not in[b['code']for b in same.basket_snapshot]
    assert worker.handle(run_id)=='ready'
    with Session()as s:
        assert len(list(s.scalars(select(AnalysisRun))))==1
        assert s.get(AnalysisRun,run_id).state=='ready'
