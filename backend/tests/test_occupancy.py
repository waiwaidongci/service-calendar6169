"""占用去重分摊引擎的离线测试，仅用标准库：python3 -m tests.test_occupancy"""
import random
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from src.services.occupancy import (
    build_events, occupancy_pipeline, initial_state, validate_occupancy,
    half_up, solo_business_minutes, case_share, EPS,
)

POLICY = {
    'version': '测试口径-v1', 'timezone': 'Asia/Shanghai',
    'weekdays': [0, 1, 2, 3, 4],
    'windows': [[540, 720], [780, 1080]],   # 09:00-12:00, 13:00-18:00
    'holidays': [], 'sla_minutes': {'普通': 480},
}
ZONE = ZoneInfo('Asia/Shanghai')
PASS, FAILS = 0, []


def check(name, cond, detail=''):
    global PASS
    if cond:
        PASS += 1
    else:
        FAILS.append((name, detail))
        print('FAIL', name, detail)


def case(nk, dept, a, b, period='2026-11', priority='普通'):
    return {'natural_key': nk, 'period': period, 'department': dept,
            'opened': a, 'closed': b, 'priority': priority}


def iso(dt):
    return dt.isoformat()


def t(h, m=0, day=10):
    return datetime(2026, 11, day, h, m, tzinfo=ZONE)


def test_half_up():
    check('half_up 0.4', half_up(0.4) == 0)
    check('half_up 0.5', half_up(0.5) == 1)
    check('half_up 1.5', half_up(1.5) == 2)
    check('half_up 2.5', half_up(2.5) == 3)
    check('half_up 负数邻近0', half_up(-0.5) == 0)
    check('half_up 浮点0.4999999999', half_up(0.4999999999) == 0)


def test_single_case_full_window():
    # 2026-11-10 是周二；09:00-18:00 全在途，去重 480 分钟，全归一组
    sources = [case('k1', '甲组', iso(t(9)), iso(t(18)))]
    occ, _ = occupancy_pipeline(sources, POLICY)
    validate_occupancy(occ)
    check('单组去重分钟=480', occ['global_minutes'] == 480, occ['global_minutes'])
    check('单组明细=480', occ['apportion'][0]['占用分钟'] == 480, occ['apportion'])
    check('日期合计=480', occ['date_totals'][0]['占用分钟'] == 480)
    check('段数=2（两个时窗）', occ['segment_count'] == 2, occ['segment_count'])


def test_overlap_equal_share():
    # 甲乙两组各一单，同时 09:00-10:00 完全重叠；去重60，各30
    sources = [case('a', '甲组', iso(t(9)), iso(t(10))),
               case('b', '乙组', iso(t(9)), iso(t(10)))]
    occ, state = occupancy_pipeline(sources, POLICY)
    validate_occupancy(occ)
    check('重叠去重=60', occ['global_minutes'] == 60, occ['global_minutes'])
    got = {r['小组']: r['占用分钟'] for r in occ['apportion']}
    check('均分各30', got == {'甲组': 30, '乙组': 30}, got)
    check('raw=60', abs(occ['raw_minutes'] - 60.0) < EPS)


def test_overlap_2_to_1_share():
    # 甲组两单、乙组一单同时 09:00-10:00；去重60，甲40 乙20
    sources = [case('a1', '甲组', iso(t(9)), iso(t(10))),
               case('a2', '甲组', iso(t(9)), iso(t(10))),
               case('b1', '乙组', iso(t(9)), iso(t(10)))]
    occ, _ = occupancy_pipeline(sources, POLICY)
    validate_occupancy(occ)
    got = {r['小组']: r['占用分钟'] for r in occ['apportion']}
    check('2:1 分摊 40/20', got == {'甲组': 40, '乙组': 20}, got)


def test_partial_overlap_dedup():
    # 甲 09:00-09:45，乙 09:15-10:00；并集 09:00-10:00=60
    # 09:00-09:15 甲独占15；09:15-09:45 各一30/2=15；09:45-10:00 乙独占15
    sources = [case('a', '甲组', iso(t(9)), iso(t(9, 45))),
               case('b', '乙组', iso(t(9, 15)), iso(t(10)))]
    occ, state = occupancy_pipeline(sources, POLICY)
    validate_occupancy(occ)
    check('并集去重60', occ['global_minutes'] == 60, occ['global_minutes'])
    got = {r['小组']: r['占用分钟'] for r in occ['apportion']}
    check('各30', got == {'甲组': 30, '乙组': 30}, got)
    check('段数3', occ['segment_count'] == 3, occ['segment_count'])


def test_lunch_and_holiday_excluded():
    # 12:00-13:00 午休不占分段；2026-11-14 周六非工作日不计
    sources = [case('a', '甲组', iso(t(11)), iso(t(14)))]
    occ, _ = occupancy_pipeline(sources, POLICY)
    # 11:00-12:00=60 + 13:00-14:00=60 = 120
    check('午休扣除=120', occ['global_minutes'] == 120, occ['global_minutes'])
    sources2 = [case('b', '甲组', iso(t(9, day=14)), iso(t(12, day=14)))]
    occ2, _ = occupancy_pipeline(sources2, POLICY)
    check('非工作日0分钟', occ2['global_minutes'] == 0 and not occ2['apportion'], occ2)


def test_holiday_excluded():
    policy = dict(POLICY, holidays=['2026-11-10'])
    sources = [case('a', '甲组', iso(t(9)), iso(t(18)))]
    occ, _ = occupancy_pipeline(sources, policy)
    check('显式假日0分钟', occ['global_minutes'] == 0, occ['global_minutes'])


def test_cross_day_segments_and_date_split():
    # 跨两天：11-10 17:00-18:00 (60)，11-11 09:00-10:00 (60)，共120，按日期行分开
    sources = [case('a', '甲组', iso(t(17, day=10)), iso(t(10, day=11)))]
    occ, _ = occupancy_pipeline(sources, POLICY)
    validate_occupancy(occ)
    check('跨日全局120', occ['global_minutes'] == 120, occ['global_minutes'])
    check('两个日期行', len(occ['date_totals']) == 2, occ['date_totals'])
    check('日期各60', [r['占用分钟'] for r in occ['date_totals']] == [60, 60])


def test_largest_remainder_tie():
    # 三个小组各一单、完全同时覆盖 31 分钟；31/3=10.333 -> 10,10,11
    sources = [case(k, g + '组', iso(t(9)), iso(t(9, 31)))
               for k, g in (('a', '甲'), ('b', '乙'), ('c', '丙'))]
    occ, _ = occupancy_pipeline(sources, POLICY)
    validate_occupancy(occ)
    check('31分钟守恒', occ['global_minutes'] == 31, occ['global_minutes'])
    vals = sorted(r['占用分钟'] for r in occ['apportion'])
    check('10/10/11', vals == [10, 10, 11], vals)
    # 同余按组名升序：名称排序后第一个（丙/甲按字典序）拿余数
    names = sorted(r['小组'] for r in occ['apportion'])
    bonus = [r['小组'] for r in occ['apportion'] if r['占用分钟'] == 11]
    check('同余组名升序补余数', bonus == [names[0]], (bonus, names))
    check('确定性复算', occupancy_pipeline(sources, POLICY)[0]['apportion'] == occ['apportion'])


def test_resume_matches_full_run():
    # 甲乙重叠 09:00-12:00；在事件中途落检查点后续跑，结果必须与一次跑完一致
    import copy
    sources = [case('a', '甲组', iso(t(9)), iso(t(12))),
               case('b', '乙组', iso(t(9, 30)), iso(t(12)))]
    full, _ = occupancy_pipeline(sources, POLICY)
    events, _, _ = build_events(sources, POLICY)

    saved = []

    def hook(state, pending):
        saved.append(copy.deepcopy(state))
        raise RuntimeError('pause')

    # 在处理第 4 个事件后崩溃，此时至少有一单在途（跨段在途单）
    try:
        occupancy_pipeline(sources, POLICY, on_checkpoint=hook, chunk_size=4)
    except RuntimeError:
        pass
    check('中途检查点已落盘', len(saved) == 1 and saved[0]['next_index'] == 4, saved)
    check('检查点接回跨段在途单', len(saved[0]['active']) >= 1, saved[0]['active'])
    cp = saved[0]
    resumed, st2 = occupancy_pipeline(sources, POLICY, checkpoint=cp)
    validate_occupancy(resumed)
    check('续跑全局值一致', resumed['global_minutes'] == full['global_minutes'], (resumed['global_minutes'], full['global_minutes']))
    check('续跑分摊明细一致', resumed['apportion'] == full['apportion'])
    check('续跑raw一致', abs(resumed['raw_minutes'] - full['raw_minutes']) < EPS)
    check('续跑段数一致', resumed['segment_count'] == full['segment_count'])
    check('无跨段在途单残留', st2['active'] == {} and st2['group_counts'] == {})
    check('事件总数符合预期', len(events) >= 6)


def test_resume_with_persisted_checkpoint():
    # 更真实：回调把状态序列化/反序列化（JSON），验证跨段在途单接回
    import json
    sources = [case('a', '甲组', iso(t(9)), iso(t(18))),
               case('b', '乙组', iso(t(9, 30)), iso(t(10, 30)))]
    full, _ = occupancy_pipeline(sources, POLICY)
    saved = []

    def hook(state, pending):
        saved.append(json.loads(json.dumps(state)))
        raise RuntimeError('simulated worker crash')

    crashed_at = None
    try:
        occupancy_pipeline(sources, POLICY, on_checkpoint=hook, chunk_size=3)
    except RuntimeError:
        crashed_at = saved[-1]
    check('崩溃时存在检查点', crashed_at is not None)
    check('崩溃检查点接回跨段在途单', len(crashed_at['active']) >= 1, crashed_at['active'])
    # 续跑：同一检查点重复两次也必须得到同一结果（重试不重复分摊）
    r1, s1 = occupancy_pipeline(sources, POLICY, checkpoint=crashed_at)
    r2, s2 = occupancy_pipeline(sources, POLICY, checkpoint=crashed_at)
    check('同检查点重试结果一致', r1['apportion'] == r2['apportion'] and r1['global_minutes'] == r2['global_minutes'])
    check('续跑等于一次跑完', r1['apportion'] == full['apportion'] and r1['global_minutes'] == full['global_minutes'])
    validate_occupancy(r1)


def test_idempotent_segment_ordinals():
    sources = [case('a', '甲组', iso(t(9)), iso(t(10))), case('b', '乙组', iso(t(9)), iso(t(10)))]
    seg_count = {'n': 0}
    seen = []

    def hook(state, pending):
        for s in pending:
            seen.append(s['ordinal'])
    occ, st = occupancy_pipeline(sources, POLICY, on_checkpoint=hook, chunk_size=1)
    check('段序号不重复', len(seen) == len(set(seen)) and seen == list(range(len(seen))), seen)


def test_brute_force_minute_grid():
    # 随机生成若干单，逐分钟枚举去重分摊作为真值，对照扫描线结果（整数守恒+精确值）
    rng = random.Random(7)
    groups = ['G1', 'G2', 'G3']
    sources = []
    for i in range(40):
        sh, sm = rng.randint(9, 16), rng.choice([0, 15, 30, 45])
        dur = rng.randint(1, 8) * 15
        start = t(sh, sm)
        sources.append(case('k%d' % i, rng.choice(groups), iso(start), iso(start.replace() )))
        sources[-1]['closed'] = iso(datetime.fromtimestamp(start.timestamp() + dur * 60, tz=ZONE))
    occ, state = occupancy_pipeline(sources, POLICY)
    validate_occupancy(occ)
    # 真值：逐分钟（每分钟起点落在时窗内且有在途单则计1去重分钟，按组计数分摊累计精确值）
    grid = {}   # (date, group) -> exact
    raw = 0.0
    start = t(9)
    for mi in range(0, 9 * 60):
        cur = datetime.fromtimestamp(start.timestamp() + mi * 60, tz=ZONE)
        nxt = datetime.fromtimestamp(start.timestamp() + (mi + 1) * 60, tz=ZONE)
        in_window = solo_business_minutes(iso(cur), iso(nxt), POLICY) >= 1 - EPS
        if not in_window:
            continue
        active_groups = {}
        for c in sources:
            if c['opened'] <= iso(cur) and c['closed'] >= iso(nxt):
                active_groups[c['department']] = active_groups.get(c['department'], 0) + 1
        if not active_groups:
            continue
        raw += 1.0
        n = sum(active_groups.values())
        d = cur.date().isoformat()
        for g, cnt in active_groups.items():
            grid[(d, g)] = grid.get((d, g), 0.0) + cnt / n
    check('逐分钟真值 raw 一致', abs(raw - state['raw_total']) < 1e-6, (raw, state['raw_total']))
    for (d, g), exact in grid.items():
        got = next(r for r in occ['apportion'] if r['日期'] == d and r['小组'] == g)
        check('精确分摊 %s/%s' % (d, g), abs(got['精确分钟'] - round(exact, 4)) < 1e-3, (got, exact))
    check('真值全局四舍五入一致', occ['global_minutes'] == half_up(raw), (occ['global_minutes'], raw))


def test_100k_scale_performance():
    # 十万单并发：验证分段算法性能与内存，不逐分钟展开
    rng = random.Random(42)
    groups = ['建模组', '采样组', '核验组', '交付组']
    sources = []
    base = t(9)
    for i in range(100000):
        off = rng.randint(0, 480)
        dur = rng.randint(30, 240)
        a = datetime.fromtimestamp(base.timestamp() + off * 60, tz=ZONE)
        b = datetime.fromtimestamp(a.timestamp() + dur * 60, tz=ZONE)
        sources.append(case('k%06d' % i, groups[i % len(groups)], iso(a), iso(b)))
    t0 = time.time()
    cps = 0

    def hook(state, pending):
        nonlocal cps
        cps += 1

    occ, state = occupancy_pipeline(sources, POLICY, on_checkpoint=hook, chunk_size=20000)
    elapsed = time.time() - t0
    validate_occupancy(occ)
    check('10万单在30秒内完成', elapsed < 30, '%.2fs' % elapsed)
    check('10万单全局分钟>0', occ['global_minutes'] > 0)
    check('10万单分段数与事件同阶', occ['segment_count'] < 400000, occ['segment_count'])
    check('10万单检查点被触发', cps > 0, cps)
    # 完全落在午休/非营业时段的单不参与分摊，其余单必须全覆盖
    intersect = [c for c in sources if solo_business_minutes(c['opened'], c['closed'], POLICY) > EPS]
    check('10万单在窗单case_frac全覆盖', len(state['case_frac']) == len(intersect),
          (len(state['case_frac']), len(intersect)))
    zero = [c for c in sources if solo_business_minutes(c['opened'], c['closed'], POLICY) <= EPS]
    check('10万单零交集告警计数一致', any(('%d 单' % len(zero)) in w for w in occ['warnings']),
          occ['warnings'])
    check('结束无在途残留', not state['active'])
    print('   10万单耗时 %.2fs，分段 %d，检查点 %d 次' % (elapsed, occ['segment_count'], cps))


def test_publish_gate_rejects_bad():
    sources = [case('a', '甲组', iso(t(9)), iso(t(10)))]
    occ, _ = occupancy_pipeline(sources, POLICY)
    bad = dict(occ)
    bad['global_minutes'] += 1
    try:
        validate_occupancy(bad)
        check('篡改全局值被拒', False)
    except ValueError:
        check('篡改全局值被拒', True)
    try:
        validate_occupancy({'global_minutes': 1})
        check('缺明细被拒', False)
    except ValueError:
        check('缺明细被拒', True)


def test_case_share_trace():
    sources = [case('a', '甲组', iso(t(9)), iso(t(10))),
               case('b', '甲组', iso(t(9)), iso(t(10))),
               case('c', '乙组', iso(t(9, 30)), iso(t(10)))]
    occ, state = occupancy_pipeline(sources, POLICY)
    # 手工构造 segments 风格行做追溯函数检查
    segments = [
        {'ordinal': 0, 'start_at': iso(t(9)), 'end_at': iso(t(9, 30)), 'seconds': 1800,
         'total_active': 2, 'groups': {'甲组': 2}},
        {'ordinal': 1, 'start_at': iso(t(9, 30)), 'end_at': iso(t(10)), 'seconds': 1800,
         'total_active': 3, 'groups': {'甲组': 2, '乙组': 1}},
    ]
    exact, touched = case_share(segments, '甲组', sources[0]['opened'], sources[0]['closed'])
    # 30*2/2 + 30*2/3 = 30 + 20 = 50
    check('逐笔追溯精确=50', abs(exact - 50.0) < EPS, exact)
    check('逐笔追溯段数=2', touched == 2, touched)
    exact_c, touched_c = case_share(segments, '乙组', sources[2]['opened'], sources[2]['closed'])
    check('乙组追溯=10', abs(exact_c - 10.0) < EPS, exact_c)


def test_multiday_cross_window_resume():
    # 跨3天、跨午休、3组错峰在途；中途崩溃（JSON检查点）后续跑，两级分摊须一致
    import json
    sources = [
        case('a', '甲组', '2026-11-10T11:30:00+08:00', '2026-11-12T10:30:00+08:00'),
        case('b', '乙组', '2026-11-10T13:00:00+08:00', '2026-11-11T17:00:00+08:00'),
        case('c', '丙组', '2026-11-11T09:00:00+08:00', '2026-11-12T18:00:00+08:00'),
    ]
    full, _ = occupancy_pipeline(sources, POLICY)
    validate_occupancy(full)
    saved = []

    def hook(state, pending):
        saved.append(json.loads(json.dumps(state)))
        raise RuntimeError('crash')

    try:
        occupancy_pipeline(sources, POLICY, on_checkpoint=hook, chunk_size=3)
    except RuntimeError:
        pass
    check('跨日崩溃接回在途单', len(saved[-1]['active']) >= 1, saved[-1]['active'])
    resumed, _ = occupancy_pipeline(sources, POLICY, checkpoint=saved[-1])
    validate_occupancy(resumed)
    check('跨日续跑全局一致', resumed['global_minutes'] == full['global_minutes'])
    check('跨日续跑小组分摊一致', resumed['apportion'] == full['apportion'])
    check('跨日续跑日期合计一致', resumed['date_totals'] == full['date_totals'])
    check('跨日续跑段数一致', resumed['segment_count'] == full['segment_count'])
    for d in full['date_totals']:
        s = sum(r['占用分钟'] for r in full['apportion'] if r['日期'] == d['日期'])
        check('日期守恒 ' + d['日期'], s == d['占用分钟'])


def test_no_business_minutes_warning():
    # 单子完全落在非工作日
    sources = [case('a', '甲组', iso(t(9, day=15)), iso(t(10, day=15)))]  # 2026-11-15 周日
    occ, _ = occupancy_pipeline(sources, POLICY)
    check('无交集全局0', occ['global_minutes'] == 0)
    check('无交集告警', any('无交集' in w for w in occ['warnings']), occ['warnings'])


if __name__ == '__main__':
    for fn in [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]:
        fn()
    print('PASS %d / %d' % (PASS, PASS + len(FAILS)))
    if FAILS:
        raise SystemExit(1)
