"""占用去重分摊的持久化编排：检查点、幂等分段、发布闸门、中断续跑。"""
import json
import time

from src.models.run import (
    OccupancyCheckpoint, OccupancySegment, OccupancyApportion, OccupancyTrace,
)
from src.services.occupancy import (
    occupancy_pipeline, validate_occupancy, solo_business_minutes,
)

CHUNK_EVENTS = 5000


def _get_checkpoint(db, run_id):
    obj = db.query(OccupancyCheckpoint).filter_by(run_id=run_id).first()
    if obj is None:
        return None
    return json.loads(obj.state) if isinstance(obj.state, str) else obj.state


def _persist(db, run_id, state, pending_segments):
    """检查点与本批重叠段同事务落盘；ordinal 唯一约束保证重试不重复分摊。"""
    cp = db.query(OccupancyCheckpoint).filter_by(run_id=run_id).first()
    payload = json.loads(json.dumps(state, default=str))
    if cp is None:
        cp = OccupancyCheckpoint(run_id=run_id, state=payload, updated=time.time())
        db.add(cp)
    else:
        cp.state = payload
        cp.updated = time.time()
    for seg in pending_segments:
        exists = db.query(OccupancySegment).filter_by(run_id=run_id, ordinal=seg['ordinal']).first()
        if exists:
            # 同检查点重试：该段已持久化，绝不重复写入
            continue
        day = seg['start_at'][:10]
        db.add(OccupancySegment(
            run_id=run_id, ordinal=seg['ordinal'], day=day,
            start_at=seg['start_at'], end_at=seg['end_at'], seconds=seg['seconds'],
            total_active=seg['total_active'], groups=json.loads(json.dumps(seg['groups'])),
        ))
    db.flush()


def _clear_published(db, run_id):
    """续跑重算前清掉上一轮发布物，避免新旧明细并存（检查点之前的段保留不重算）。"""
    db.query(OccupancyApportion).filter_by(run_id=run_id).delete()
    db.query(OccupancyTrace).filter_by(run_id=run_id).delete()


def run_occupancy(db, run, chunk_events=CHUNK_EVENTS):
    """对一个 Run 执行占用核算；返回结果字典，失败时抛出由 worker 捕获的异常。

    chunk_events 仅影响检查点频率，不影响结果，便于小样本测试中断续跑。
    """
    raw_sources = run.snapshot['sources']
    policy = run.snapshot['policy']
    # 用冻结的自然键为占用引擎补 natural_key；历史快照无该字段时回退为序号，保证可续跑
    frozen_keys = run.snapshot.get('natural_keys') or []
    sources = [
        dict(src, natural_key=(frozen_keys[i] if i < len(frozen_keys) else src.get('natural_key', str(i + 1))))
        for i, src in enumerate(raw_sources)
    ]
    checkpoint = _get_checkpoint(db, run.id)
    _clear_published(db, run.id)

    def on_checkpoint(state, pending_segments):
        _persist(db, run.id, state, pending_segments)
        db.commit()

    occ, state = occupancy_pipeline(
        sources, policy, checkpoint=checkpoint,
        on_checkpoint=on_checkpoint, chunk_size=chunk_events,
    )
    # 发布闸门：全局值和分摊明细齐全且守恒才落库发布
    validate_occupancy(occ)
    _persist(db, run.id, state, [])  # 终态检查点（next_index 到末尾，active 清空）
    for r in occ['apportion']:
        db.add(OccupancyApportion(
            run_id=run.id, day=r['日期'], group_name=r['小组'],
            minutes=r['占用分钟'], exact_minutes=r['精确分钟'], peak_active=r['在途峰值'],
        ))
    for idx, src in enumerate(sources):
        exact = state['case_frac'].get(idx, 0.0)
        touched = state['case_segments'].get(idx, 0)
        solo = solo_business_minutes(src['opened'], src['closed'], policy)
        db.add(OccupancyTrace(
            run_id=run.id, natural_key=src.get('natural_key', str(idx + 1)),
            period=src['period'], group_name=src['department'], priority=src['priority'],
            opened=src['opened'], closed=src['closed'],
            exact_minutes=round(exact, 4), solo_minutes=round(solo, 4), segment_count=touched,
        ))
    db.commit()
    # 全量重叠段与逐笔追溯已落专表，经分页端点查询；结果JSON只保留计数，避免十万单撑爆字段
    occ['segment_persisted'] = db.query(OccupancySegment).filter_by(run_id=run.id).count()
    occ['trace_count'] = len(sources)
    return occ


def query_segments(db, run_id, day=None, limit=200, offset=0):
    q = db.query(OccupancySegment).filter_by(run_id=run_id)
    if day:
        q = q.filter(OccupancySegment.day == day)
    total = q.count()
    rows = q.order_by(OccupancySegment.ordinal).limit(limit).offset(offset).all()
    return total, [{
        '序号': s.ordinal, '日期': s.day, '开始': s.start_at[11:19], '结束': s.end_at[11:19],
        '秒': s.seconds, '去重分钟': round(s.seconds / 60.0, 4),
        '在途单数': s.total_active, '各组在途': json.dumps(s.groups, ensure_ascii=False),
    } for s in rows]


def query_apportion(db, run_id):
    rows = db.query(OccupancyApportion).filter_by(run_id=run_id).order_by(
        OccupancyApportion.day, OccupancyApportion.group_name).all()
    return [{'日期': r.day, '小组': r.group_name, '占用分钟': r.minutes,
             '精确分钟': r.exact_minutes, '在途峰值': r.peak_active} for r in rows]


def query_trace(db, run_id, natural_key=None, group=None, limit=100, offset=0):
    q = db.query(OccupancyTrace).filter_by(run_id=run_id)
    if natural_key:
        q = q.filter(OccupancyTrace.natural_key == natural_key)
    if group:
        q = q.filter(OccupancyTrace.group_name == group)
    total = q.count()
    rows = q.order_by(OccupancyTrace.id).limit(limit).offset(offset).all()
    return total, [{
        '底稿键': r.natural_key, '期间': r.period, '小组': r.group_name, '优先级': r.priority,
        '开始': r.opened, '结束': r.closed,
        '去重分摊分钟': r.exact_minutes, '未去重交集分钟': r.solo_minutes, '参与重叠段': r.segment_count,
    } for r in rows]


def published_dates(db, run_id):
    return [r[0] for r in db.query(OccupancySegment.day).filter_by(run_id=run_id)
            .distinct().order_by(OccupancySegment.day).all()]
