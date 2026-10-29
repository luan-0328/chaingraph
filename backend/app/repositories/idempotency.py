"""消息和事件的原子去重：唯一键冲突只回滚 SAVEPOINT，保留外层事务。"""
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError


def insert_once(db, model, *, dedup_key: str | None, **fields):
    """返回 (记录, 是否首次插入)；其他约束错误不当作重复吞掉。"""
    row = model(dedup_key=dedup_key, **fields)
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        if dedup_key is None:
            raise
        # 唯一键冲突已持有共享锁；不能再升级为排他锁，否则多个重复请求会互相等待。
        # 共享锁读取同样是 MySQL 当前读，不会读到事务开始时的旧快照。
        row = db.scalar(select(model).where(model.dedup_key == dedup_key).with_for_update(read=True))
        if row is None:
            raise
        return row, False
    db.refresh(row)
    return row, True
