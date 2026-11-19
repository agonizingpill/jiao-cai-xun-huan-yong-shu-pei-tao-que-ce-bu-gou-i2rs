import conftest  # noqa: F401
import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.db import Base,engine,Session
from src import seed_service,worker
from src.models import Pack,PackLine,Edition,Classroom

PASSWORD='Books2026!'


@pytest.fixture()
def client(monkeypatch):
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    fake=fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(worker,'q',fake)
    seed_service.seed()
    import src.main as main
    monkeypatch.setattr(main,'q',fake)
    with TestClient(main.app)as c:
        c.fake=fake
        yield c


def login(client,email):
    r=client.post('/auth/login',json={'email':email,'password':PASSWORD})
    assert r.status_code==200,r.text
    return {'Authorization':'Bearer '+r.json()['token']}


def make_pack(code='PACK-API-1'):
    with Session()as s:
        room=s.scalar(select(Classroom).where(Classroom.code=='G1-3'))
        phy=s.scalar(select(Edition).where(Edition.code=='PHY-V2'))
        lab=s.scalar(select(Edition).where(Edition.code=='LAB-V2'))
        p=Pack(code=code,classroom_id=room.id,term='2026秋');s.add(p);s.flush()
        s.add_all([PackLine(pack_id=p.id,edition_id=phy.id,quantity=2),PackLine(pack_id=p.id,edition_id=lab.id,quantity=2)])
        s.commit()
        return p.id


def test_full_run_lifecycle_over_api(client):
    op=login(client,'operator@xunshu.demo');sup=login(client,'supervisor@xunshu.demo');view=login(client,'viewer@xunshu.demo')
    pack_id=make_pack()
    # viewer cannot start analysis
    assert client.post(f'/packs/{pack_id}/recommend',headers=view).status_code==403
    r=client.post(f'/packs/{pack_id}/recommend',headers=op)
    assert r.status_code==200,r.text
    body=r.json();run_id=body['run']['id'];assert body['mode']=='created'
    # a duplicate click does not create a second run
    assert client.post(f'/packs/{pack_id}/recommend',headers=op).status_code==409
    assert client.fake.llen('book-baskets')==1
    assert worker.handle(run_id)=='ready'
    # the page can see which attempt is computing/finished and which baskets were used
    r=client.get(f'/packs/{pack_id}/analysis',headers=view)
    data=r.json();assert data['active_run_id']==run_id
    run=data['runs'][0];assert run['state']=='ready'
    assert run['basket_count']==96 and len(run['basket_codes'])==96
    assert set(run['input_subjects'])=={'physics','lab'}
    assert run['basket_codes'][0].startswith('HIST-')
    arch=client.get('/archive',headers=view).json()
    assert arch['analysis_runs'][0]['id']==run_id
    assert 'recommendation'not in arch['analysis_runs'][0]
    assert arch['packs'][0]['active_run_id']==run_id


def test_abandon_requires_writer_and_allows_fresh_start(client):
    op=login(client,'operator@xunshu.demo');view=login(client,'viewer@xunshu.demo')
    pack_id=make_pack('PACK-API-2')
    run_id=client.post(f'/packs/{pack_id}/recommend',headers=op).json()['run']['id']
    with Session()as s:
        assert worker.claim(s,run_id)is not None
        s.commit()
    assert client.post(f'/packs/{pack_id}/analysis/abandon',json={'reason':'卡住'},headers=view).status_code==403
    r=client.post(f'/packs/{pack_id}/analysis/abandon',json={'reason':'清洗样本'},headers=op)
    assert r.status_code==200,r.text
    assert r.json()['run']['state']=='abandoned'
    # supervisor can also restart
    sup=login(client,'supervisor@xunshu.demo')
    r=client.post(f'/packs/{pack_id}/recommend',headers=sup)
    assert r.status_code==200 and r.json()['mode']=='created'
    second=r.json()['run']['id'];assert second!=run_id
    runs=client.get(f'/packs/{pack_id}/analysis',headers=op).json()['runs']
    assert [(x['attempt'],x['state'])for x in runs]==[(1,'abandoned'),(2,'queued')]


def test_retry_same_run_is_idempotent_over_api(client):
    op=login(client,'operator@xunshu.demo')
    pack_id=make_pack('PACK-API-3')
    run_id=client.post(f'/packs/{pack_id}/recommend',headers=op).json()['run']['id']
    with Session()as s:
        worker.claim(s,run_id);worker.fail(s,run_id,RuntimeError('db down'));s.commit()
    r=client.post(f'/packs/{pack_id}/recommend',headers=op)
    assert r.status_code==200 and r.json()['mode']=='retry'and r.json()['run']['id']==run_id
    assert worker.handle(run_id)=='ready'
    runs=client.get(f'/packs/{pack_id}/analysis',headers=op).json()['runs']
    assert len(runs)==1 and runs[0]['state']=='ready'
    # finished analysis cannot be re-run as the same attempt
    assert client.post(f'/packs/{pack_id}/recommend',headers=op).status_code==409
