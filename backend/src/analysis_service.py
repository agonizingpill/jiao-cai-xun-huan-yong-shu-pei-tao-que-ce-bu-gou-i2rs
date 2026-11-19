from sqlalchemy import select,func
from fastapi import HTTPException
from .models import Pack,PackLine,Edition,HistoricalBasket,AnalysisRun,utcnow
from .common import lock,pack,audit

ACTIVE=('queued','running')


def snapshot_baskets(s):
    return [{'code':b.code,'subjects':list(b.subjects)} for b in s.scalars(select(HistoricalBasket).order_by(HistoricalBasket.id))]


def snapshot_editions(s,pack_id):
    rows=s.execute(select(PackLine,Edition).join(Edition,PackLine.edition_id==Edition.id).where(PackLine.pack_id==pack_id).order_by(PackLine.id)).all()
    return [{'pack_line_id':line.id,'edition_id':edition.id,'code':edition.code,'subject':edition.subject,'version':edition.version}for line,edition in rows]


def current_run(s,pack):
    if not pack.active_run_id:return None
    return s.scalar(select(AnalysisRun).where(AnalysisRun.id==pack.active_run_id))


def start(s,pack_id,u):
    """Create one analysis run, or retry the same failed run with its original snapshot.

    The snapshot freezes both the historical basket batch and the pack subjects, so a
    retry after a crash keeps the inputs of the first attempt.
    """
    p=lock(s,Pack,pack_id)
    run=current_run(s,p)
    if run and run.state in ACTIVE:raise HTTPException(409,'共需分析正在进行，请等待完成或先放弃本次分析')
    if run and run.state=='ready':raise HTTPException(409,'本次配套已有分析建议，同一次分析不重复生成')
    if run and run.state=='failed':
        run.state='queued';run.error=None;run.started_at=None;run.finished_at=None;run.requested_by=u.email
        p.ml_state='queued';p.error=None
        audit(s,u,'重试同一次共需分析 #'+str(run.id));s.flush()
        return p,run,'retry'
    attempt=(s.scalar(select(func.max(AnalysisRun.attempt)).where(AnalysisRun.pack_id==pack_id))or 0)+1
    run=AnalysisRun(pack_id=pack_id,attempt=attempt,state='queued',requested_by=u.email,
                    edition_snapshot=snapshot_editions(s,pack_id),basket_snapshot=snapshot_baskets(s))
    s.add(run);s.flush()
    p.active_run_id=run.id;p.ml_state='queued';p.error=None
    audit(s,u,'发起第'+str(attempt)+'次共需分析 '+str(p.code));s.flush()
    return p,run,'created'


def view(run):
    """Compact run record for the archive/page: what was analysed and what came out."""
    result=run.recommendation or {}
    return {'id':run.id,'pack_id':run.pack_id,'attempt':run.attempt,'state':run.state,
            'requested_by':run.requested_by,'error':run.error,
            'created_at':run.created_at,'started_at':run.started_at,'finished_at':run.finished_at,
            'input_subjects':[e['subject']for e in run.edition_snapshot],
            'editions':[{'pack_line_id':e['pack_line_id'],'edition_id':e['edition_id'],'code':e['code'],'subject':e['subject'],'version':e['version']}for e in run.edition_snapshot],
            'basket_codes':[b['code']for b in run.basket_snapshot],
            'basket_count':len(run.basket_snapshot),
            'suggestions':result.get('suggestions',[]),
            'holdout_hidden_item_recall':result.get('holdout_hidden_item_recall'),
            'holdout_recommendation_coverage':result.get('holdout_recommendation_coverage')}


def detail(run):
    return view(run)|{'baskets':run.basket_snapshot,'recommendation':run.recommendation}


def abandon(s,pack_id,d,u):
    p=lock(s,Pack,pack_id)
    run=current_run(s,p)
    if not run:raise HTTPException(409,'没有可放弃的共需分析')
    run=lock(s,AnalysisRun,run.id)
    if run.state in ('ready','abandoned','superseded'):raise HTTPException(409,'该次分析已经结束，不能放弃')
    if run.state not in ('queued','running','failed'):raise HTTPException(409,'该次分析不在可放弃状态')
    run.state='abandoned';run.finished_at=utcnow();run.error=(d.reason or '库管放弃本次分析')[:450]
    p.ml_state='abandoned';p.error='库管已放弃第'+str(run.attempt)+'次分析，可另开一次'
    audit(s,u,'放弃第'+str(run.attempt)+'次共需分析：'+(d.reason or '未说明'));s.flush()
    return p,run
