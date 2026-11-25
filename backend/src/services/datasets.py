import csv
import hashlib
import io
import json
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from src.domain.demo import demo_rows
from src.domain.spec import SPEC
from src.models.source import Dataset


def checksum(rows):
    raw = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def save(db, name, filename, rows, actor):
    signature = checksum(rows)
    # Serialize imports of the same batch so versions stay monotonic and only one stays current:
    # lock the lineage first, then read checksum and version inside that lock.
    db.scalars(select(Dataset.id).where(Dataset.name == name).order_by(Dataset.id).with_for_update()).all()
    lookup = select(Dataset).where(Dataset.name == name, Dataset.checksum == signature)
    existing = db.scalar(lookup)
    if existing:
        return existing, False
    previous = db.scalar(select(Dataset).where(Dataset.name == name, Dataset.current.is_(True))
                         .order_by(Dataset.version.desc()).limit(1))
    version = db.scalar(select(func.max(Dataset.version)).where(Dataset.name == name)) or 0
    dataset = Dataset(name=name, filename=filename, checksum=signature, rows=rows,
                      row_count=len(rows), version=version + 1, current=True,
                      replaces_id=previous.id if previous else None, author=actor)
    try:
        with db.begin_nested():
            if previous:
                previous.current = False
            db.add(dataset)
            db.flush()
    except IntegrityError:
        existing = db.scalar(lookup.with_for_update())
        if existing:
            return existing, False
        raise
    return dataset, True


def template():
    fields = [field['key'] for field in SPEC['fields']]
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    # The complete representative dataset also works as a reproducible import fixture.
    writer.writerows(demo_rows())
    return '﻿' + buffer.getvalue()
