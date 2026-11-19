"""occupancy_run 持久化编排的离线集成测试。

本地无 SQLAlchemy/Redis，用标准库内存假库替换 src.models.run 与 src.config.database，
覆盖：检查点落盘、重叠段幂等写入、模拟崩溃后续跑、发布闸门、重复运行不重复分摊、分页筛选。
"""
import sys
import types
import copy
from datetime import datetime
from zoneinfo import ZoneInfo

# ---- 最小内存假 ORM：仅实现 occupancy_run 实际使用的查询面 ----
class _Column:
    def __init__(self, model, name):
        self.model = model
        self.name = name

    def __eq__(self, other):
        return (self.name, other)

    def __hash__(self):
        return hash((self.model, self.name))


class _Expr:
    def __init__(self, key, value):
        self.key = key
        self.value = value


class _Query:
    def __init__(self, table, rows, column=None):
        self._table = table
        self._rows = rows
        self._column = column
        self._filters = {}
        self._order = None
        self._limit = None
        self._offset = 0
        self._distinct = False

    def filter_by(self, **kw):
        self._filters.update(kw)
        return self

    def filter(self, *exprs):
        for key, value in exprs:
            self._filters[key] = value
        return self

    def order_by(self, *keys):
        self._order = [(k.name if isinstance(k, _Column) else k) for k in keys]
        return self

    def limit(self, n):
        self._limit = n
        return self

    def offset(self, n):
        self._offset = n
        return self

    def distinct(self):
        self._distinct = True
        return self

    def _match(self, inst):
        return all(getattr(inst, k, None) == v for k, v in self._filters.items())

    def _eval(self):
        rows = [r for r in self._rows if self._match(r)]
        if self._order is not None:
            keys = self._order
            rows = sorted(rows, key=lambda x: tuple(getattr(x, k) for k in keys))
        if self._distinct:
            seen, out = set(), []
            for r in rows:
                v = getattr(r, self._column) if self._column else r
                if v not in seen:
                    seen.add(v)
                    out.append(r)
            rows = out
        if self._offset:
            rows = rows[self._offset:]
        if self._limit is not None:
            rows = rows[:self._limit]
        return rows

    def all(self):
        rows = self._eval()
        if self._column:
            return [getattr(r, self._column) for r in rows]
        return rows

    def first(self):
        rows = self._eval()
        return rows[0] if rows else None

    def count(self):
        return len(self._eval())

    def delete(self):
        doomed = self._eval()
        for r in doomed:
            self._rows.remove(r)
        return len(doomed)


class FakeDB:
    def __init__(self):
        self.tables = {}
        self.commits = 0
        self.fail_commits = None  # 第N次commit时抛错模拟崩溃
        self._snapshot = {}

    def add(self, obj):
        rows = self.tables.setdefault(type(obj), [])
        if getattr(obj, 'id', None) is None:
            obj.id = 1 + max((getattr(r, 'id', 0) or 0) for r in rows) if rows else 1
        rows.append(obj)

    def query(self, entity):
        if isinstance(entity, _Column):
            # 模拟 SQLAlchemy 的 db.query(Model.column)
            rows = self.tables.setdefault(entity.model, [])
            return _Query(rows, rows, column=entity.name)
        rows = self.tables.setdefault(entity, [])
        return _Query(rows, rows)

    def get(self, model, pk):
        for obj in self.tables.get(model, []):
            if getattr(obj, 'id', None) == pk:
                return obj
        return None

    def flush(self):
        pass

    def commit(self):
        self.commits += 1
        if self.fail_commits is not None and self.commits >= self.fail_commits:
            # 模拟真实事务回滚：丢弃上次成功提交以来的全部新增
            self.tables = copy.deepcopy(self._snapshot)
            raise RuntimeError('simulated crash on commit #%d' % self.commits)
        self._snapshot = copy.deepcopy(self.tables)

    def rollback(self):
        self.tables = copy.deepcopy(self._snapshot)


class CrashDB(FakeDB):
    """在上一个成功事务已持久化至少2个重叠段后，于下一次提交崩溃并回滚。"""
    def __init__(self):
        super().__init__()
        self.fail = True

    def commit(self):
        prev_seg = sum(1 for t in self._snapshot.values() for o in t if isinstance(o, OccupancySegment))
        if self.fail and prev_seg >= 2:
            self.tables = copy.deepcopy(self._snapshot)
            raise RuntimeError('crash after segments persisted')
        super().commit()


class _Model:
    def __init__(self, **kw):
        self.id = kw.pop('id', None)
        for k, v in kw.items():
            setattr(self, k, v)


def _model(name, cols):
    cls = type(name, (_Model,), {})
    for c in cols:
        setattr(cls, c, _Column(cls, c))
    return cls


OccupancyCheckpoint = _model('OccupancyCheckpoint', ['id', 'run_id', 'state', 'updated'])
OccupancySegment = _model('OccupancySegment', ['id', 'run_id', 'ordinal', 'day', 'start_at', 'end_at', 'seconds', 'total_active', 'groups'])
OccupancyApportion = _model('OccupancyApportion', ['id', 'day', 'group_name', 'minutes', 'exact_minutes', 'peak_active', 'run_id'])
OccupancyTrace = _model('OccupancyTrace', ['id', 'run_id', 'natural_key', 'period', 'group_name', 'priority', 'opened', 'closed', 'exact_minutes', 'solo_minutes', 'segment_count'])

# ---- 在导入被测模块前注入桩 ----
fake_models = types.ModuleType('src.models.run')
fake_models.OccupancyCheckpoint = OccupancyCheckpoint
fake_models.OccupancySegment = OccupancySegment
fake_models.OccupancyApportion = OccupancyApportion
fake_models.OccupancyTrace = OccupancyTrace
fake_db_mod = types.ModuleType('src.config.database')
fake_db_mod.Base = type('Base', (), {})
sys.modules['src.models.run'] = fake_models
sys.modules['src.config.database'] = fake_db_mod

from src.services import occupancy_run  # noqa: E402

ZONE = ZoneInfo('Asia/Shanghai')
POLICY = {'version': 'v', 'timezone': 'Asia/Shanghai', 'weekdays': [0, 1, 2, 3, 4],
          'windows': [[540, 720], [780, 1080]], 'holidays': [], 'sla_minutes': {'普通': 480}}
PASS, FAILS = 0, []


def check(name, cond, detail=''):
    global PASS
    if cond:
        PASS += 1
    else:
        FAILS.append((name, detail))
        print('FAIL', name, detail)


class RunStub:
    def __init__(self, run_id, sources):
        self.id = run_id
        self.status = 'running'
        self.snapshot = {'sources': sources, 'policy': POLICY}


def mk_sources(n=60):
    sources = []
    groups = ['建模组', '采样组', '核验组']
    base = datetime(2026, 11, 10, 9, tzinfo=ZONE)
    for i in range(n):
        a = datetime.fromtimestamp(base.timestamp() + (i % 20) * 60, tz=ZONE)
        b = datetime.fromtimestamp(a.timestamp() + (90 + (i % 5) * 30) * 60, tz=ZONE)
        sources.append({'natural_key': 'k%03d' % i, 'period': '2026-11',
                        'department': groups[i % 3], 'opened': a.isoformat(),
                        'closed': b.isoformat(), 'priority': '普通'})
    return sources


def test_natural_keys_frozen_and_fallback():
    # 真实快照：业务data不含natural_key（校验只允许5字段），自然键在并行数组并受checksum覆盖
    n = 10
    business = []
    keys = []
    base = datetime(2026, 11, 10, 9, tzinfo=ZONE)
    for i in range(n):
        a = base
        b = datetime.fromtimestamp(a.timestamp() + 120 * 60, tz=ZONE)
        business.append({'period': '2026-11', 'department': '建模组',
                         'opened': a.isoformat(), 'closed': b.isoformat(), 'priority': '普通'})
        keys.append('demo-%d' % (i + 1))
    run = RunStub(11, business)
    run.snapshot['natural_keys'] = keys
    db = FakeDB()
    occ = occupancy_run.run_occupancy(db, run)
    traces = db.query(OccupancyTrace).all()
    check('追溯使用冻结自然键', [t.natural_key for t in traces] == keys)
    check('结果不受自然键影响', occ['global_minutes'] > 0)

    # 历史快照无 natural_keys：回退序号，不报错
    legacy = RunStub(12, [dict(s) for s in business])
    db2 = FakeDB()
    occupancy_run.run_occupancy(db2, legacy)
    legacy_keys = [t.natural_key for t in db2.query(OccupancyTrace).all()]
    check('旧快照回退序号', legacy_keys == [str(i + 1) for i in range(n)], legacy_keys[:3])


def test_full_run_publishes_all_tables():
    db = FakeDB()
    run = RunStub(1, mk_sources())
    occ = occupancy_run.run_occupancy(db, run)
    check('全局分钟为整数', isinstance(occ['global_minutes'], int))
    check('分摊明细非空', len(occ['apportion']) > 0)
    check('分摊守恒', sum(r['占用分钟'] for r in occ['apportion']) == occ['global_minutes'])
    check('检查点落盘1行', db.query(OccupancyCheckpoint).count() == 1)
    cp = db.query(OccupancyCheckpoint).first()
    check('终态游标到末尾', cp.state['next_index'] > 0 and not cp.state['active'])
    segs = db.query(OccupancySegment).all()
    check('重叠段全部落盘', len(segs) == occ['segment_count'], (len(segs), occ['segment_count']))
    check('全量段不内嵌结果JSON', 'segments' not in occ)
    check('段计数与专表一致', occ['segment_persisted'] == len(segs))
    check('段ordinal唯一', len({s.ordinal for s in segs}) == len(segs))
    check('段ordinal连续', sorted(s.ordinal for s in segs) == list(range(len(segs))))
    apps = db.query(OccupancyApportion).all()
    check('分摊行数=结果行数', len(apps) == len(occ['apportion']))
    check('分摊键唯一', len({(a.day, a.group_name) for a in apps}) == len(apps))
    traces = db.query(OccupancyTrace).all()
    check('逐笔追溯全覆盖', len(traces) == len(run.snapshot['sources']))
    check('追溯含未去重对照', all(hasattr(t, 'solo_minutes') for t in traces))


def test_crash_then_resume():
    # 极小检查点块，在已有重叠段落盘后崩溃；随后用同一假库续跑，结果须与一次跑完一致
    sources = mk_sources(80)
    full = occupancy_run.run_occupancy(FakeDB(), RunStub(9, sources), chunk_events=5000)

    db = CrashDB()
    crashed = False
    try:
        occupancy_run.run_occupancy(db, RunStub(7, copy.deepcopy(sources)), chunk_events=2)
    except RuntimeError:
        crashed = True
    check('崩溃被触发', crashed)
    segs_mid = db.query(OccupancySegment).count()
    check('崩溃前已有部分段落盘', segs_mid >= 2, segs_mid)
    cp_mid = db.query(OccupancyCheckpoint).first()
    check('崩溃检查点存在且接回在途单', cp_mid is not None and cp_mid.state['next_index'] > 0)

    # 续跑：同一假库、同一run，关闭故障
    db.fail = False
    occ = occupancy_run.run_occupancy(db, RunStub(7, copy.deepcopy(sources)), chunk_events=2)
    check('续跑全局值一致', occ['global_minutes'] == full['global_minutes'])
    check('续跑明细一致', occ['apportion'] == full['apportion'])
    segs = db.query(OccupancySegment).all()
    check('续跑段不重复', len(segs) == occ['segment_count'] == full['segment_count'])
    check('续跑ordinal仍唯一连续', sorted(s.ordinal for s in segs) == list(range(len(segs))))
    check('续跑检查点仅1行', db.query(OccupancyCheckpoint).count() == 1)
    check('续跑追溯全覆盖', db.query(OccupancyTrace).count() == len(sources))


def test_retry_whole_run_is_idempotent():
    db = FakeDB()
    run = RunStub(3, mk_sources(50))
    o1 = occupancy_run.run_occupancy(db, run)
    n_seg_1 = db.query(OccupancySegment).count()
    # 完整重跑（模拟worker对同一run再次消费）：发布物先清再建，段靠ordinal幂等
    o2 = occupancy_run.run_occupancy(db, run)
    check('重跑全局值稳定', o1['global_minutes'] == o2['global_minutes'])
    check('重跑段不重复', db.query(OccupancySegment).count() == n_seg_1)
    check('重跑分摊不重复', db.query(OccupancyApportion).count() == len(o2['apportion']))
    check('重跑追溯不重复', db.query(OccupancyTrace).count() == 50)
    check('检查点仍1行', db.query(OccupancyCheckpoint).count() == 1)


def test_pagination_and_filters():
    db = FakeDB()
    sources = mk_sources(100)
    occ = occupancy_run.run_occupancy(db, RunStub(5, sources))
    total, rows = occupancy_run.query_segments(db, 5, limit=10, offset=0)
    check('段分页total', total == occ['segment_count'])
    check('段分页limit', len(rows) == 10)
    _, rows2 = occupancy_run.query_segments(db, 5, limit=10, offset=10)
    check('分页不重叠', rows[0]['序号'] != rows2[0]['序号'])
    dates = occupancy_run.published_dates(db, 5)
    check('日期列表有序', dates == sorted(dates) and len(dates) >= 1)
    if dates:
        tday, drows = occupancy_run.query_segments(db, 5, day=dates[0], limit=1000)
        check('日期筛选正确', all(r['日期'] == dates[0] for r in drows) and tday == len(drows))
    tt, trows = occupancy_run.query_trace(db, 5, natural_key='k000')
    check('底稿键筛选', tt == 1 and trows[0]['底稿键'] == 'k000')
    tg, grows = occupancy_run.query_trace(db, 5, group='建模组', limit=1000)
    check('小组筛选', all(r['小组'] == '建模组' for r in grows) and tg == len(grows))
    tt2, page = occupancy_run.query_trace(db, 5, limit=20, offset=0)
    check('追溯分页', len(page) == 20 and tt2 == len(sources))
    app_rows = occupancy_run.query_apportion(db, 5)
    check('分摊查询守恒', sum(r['占用分钟'] for r in app_rows) == occ['global_minutes'])


def test_publish_gate_blocks_on_tamper():
    import src.services.occupancy as occ_engine
    db = FakeDB()
    run = RunStub(8, mk_sources(20))
    orig = occ_engine.validate_occupancy

    def bad_validate(o):
        o = dict(o)
        o['global_minutes'] = o['global_minutes'] + 1
        return orig(o)

    occ_engine.validate_occupancy = bad_validate
    try:
        raised = False
        try:
            occupancy_run.run_occupancy(db, run)
        except ValueError:
            raised = True
        check('闸门失败时不发布分摊', raised and db.query(OccupancyApportion).count() == 0)
        check('闸门失败时检查点仍保留供续跑', db.query(OccupancyCheckpoint).count() == 1)
    finally:
        occ_engine.validate_occupancy = orig
    # 闸门恢复后同run续跑成功
    occ = occupancy_run.run_occupancy(db, run)
    check('恢复后发布成功', db.query(OccupancyApportion).count() == len(occ['apportion']))


if __name__ == '__main__':
    for fn in [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]:
        fn()
    print('PASS %d / %d' % (PASS, PASS + len(FAILS)))
    if FAILS:
        raise SystemExit(1)
