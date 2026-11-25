from sqlalchemy import select
from src.constants.enums import Status
from src.models.jobs import Delivery, Run
from src.models.source import Dataset

PENDING = '计算中，完成后才可分发'
FAILED = '计算失败，请重新发起试算'
SUPERSEDED = '同批已有更晚受理的试算，本次结果不再分发'
STALE = '源数据已更新，旧试算的分发资格已失效'
BLOCKED = '换版差值未达标或分母为零，暂缓分发'
DELIVERED = '已进入月报分发，按存档餐单复算'
REPLAYED = '按存档餐单复算，结果不随源数据更新变化'
ELIGIBLE = '可进入月报分发'


def lineage_ids(db, dataset_id):
    """Every version of the batch this dataset belongs to, supplements included."""
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        return [dataset_id]
    return list(db.scalars(select(Dataset.id).where(Dataset.name == dataset.name)))


def lock_lineage(db, dataset_id):
    """Serialize concurrent trials of the same batch so only the last accepted one survives."""
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        return None
    # One ordered SELECT ... FOR UPDATE over the whole lineage; avoids lock-order deadlocks.
    db.scalars(select(Dataset.id).where(Dataset.name == dataset.name).order_by(Dataset.id)
               .with_for_update()).all()
    return dataset


def delivered_receipt(db, run_id):
    return db.scalar(select(Delivery).where(Delivery.run_id == run_id, Delivery.state == 'delivered'))


def delivered_ids(db):
    return set(db.scalars(select(Delivery.run_id).where(Delivery.state == 'delivered')))


def supersede_previous(db, run, delivered=frozenset()):
    """Only the last accepted trial on the same batch keeps distribution rights."""
    ids = lineage_ids(db, run.dataset_id)
    for other in db.scalars(select(Run).where(Run.dataset_id.in_(ids), Run.id < run.id).with_for_update()):
        if other.superseded_by is not None or other.pinned is not None:
            continue
        if other.id in delivered or delivered_receipt(db, other.id) is not None:
            continue
        other.superseded_by = run.id
        if other.status in (Status.QUEUED, Status.RUNNING):
            other.status = Status.SUPERSEDED
            other.lease_until = None
            other.error = SUPERSEDED


def state(db, job, delivered=None):
    """Distribution eligibility for one run, evaluated against current source lineage."""
    if job.status in (Status.QUEUED, Status.RUNNING):
        return {'state': 'pending', 'label': '排队计算', 'reason': PENDING}
    if job.status == Status.SUPERSEDED:
        return {'state': 'superseded', 'label': '已被覆盖', 'reason': SUPERSEDED}
    if job.status == Status.FAILED:
        return {'state': 'failed', 'label': '计算失败', 'reason': job.error or FAILED}
    if job.superseded_by is not None:
        return {'state': 'superseded', 'label': '已被覆盖', 'reason': SUPERSEDED}
    if not job.result:
        return {'state': 'failed', 'label': '无结果', 'reason': FAILED}
    # A report that already went out is frozen to the meal selection archived at delivery time.
    if (job.id in delivered if delivered is not None else delivered_receipt(db, job.id) is not None):
        return {'state': 'delivered', 'label': '已分发', 'reason': DELIVERED}
    if job.pinned is not None:
        return {'state': 'replay', 'label': '按存档复算', 'reason': REPLAYED}
    if not job.result.get('ready'):
        return {'state': 'blocked', 'label': '暂缓分发', 'reason': BLOCKED}
    dataset = db.get(Dataset, job.dataset_id)
    if dataset is None or not dataset.current:
        return {'state': 'stale', 'label': '资格失效', 'reason': STALE}
    return {'state': 'eligible', 'label': '可分发', 'reason': ELIGIBLE}


def can_distribute(db, job):
    return state(db, job)['state'] == 'eligible'
