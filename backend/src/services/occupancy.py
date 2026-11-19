"""服务占用去重分摊引擎（纯标准库，不依赖数据库与Web框架）。

口径：
1. 输入为提交时冻结的底稿与口径快照，运行期间修改源数据不影响本次核算。
2. 营业时窗与每单在途区间统一转换为口径时区后生成事件，扫描线切分重叠段，
   绝不逐分钟展开：事件数为 O(单数)，分段数与事件数同阶，可支撑十万单并发。
3. 一个重叠段的去重分钟按当时各小组在途单数占比分摊：
   小组g分得 = 段分钟 * 组g在途单数 / 段内在途单总数。
4. 全局四舍五入（半数上进，非银行家舍入）得到整数总分钟；随后两级最大余数法：
   先把全局整数分钟分到各业务日期，再把每个日期的整数分钟分到各小组；
   余数大的先补，同余按日期ISO升序、小组名称升序确定归属，保证确定性。
5. 检查点记录事件游标、窗口栈、跨段在途单与已分摊累计；中断后续跑从游标恢复，
   已分摊事件不会二次处理，因此重试不重复分摊。
"""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
import copy
import math

EPS = 1e-9
WINDOW_KIND = 0
CASE_KIND = 1


def half_up(value):
    """四舍五入：0.5 恒上进（Python 内置 round 为银行家舍入，不可直接使用）。"""
    return math.floor(value + 0.5)


def _parse(dt, zone):
    return datetime.fromisoformat(dt).astimezone(zone)


def solo_business_minutes(opened, closed, policy):
    """单笔在途单与营业时窗的交集分钟（未去重口径，供逐笔追溯展示）。"""
    zone = ZoneInfo(policy['timezone'])
    a = _parse(opened, zone)
    b = _parse(closed, zone)
    if b < a:
        raise ValueError('结束时间早于开始时间')
    total = 0.0
    day = a.date()
    while day <= b.date():
        if day.weekday() in policy['weekdays'] and day.isoformat() not in policy.get('holidays', []):
            midnight = datetime.combine(day, time(), zone)
            for ws, we in policy['windows']:
                lo = max(a, midnight + timedelta(minutes=ws))
                hi = min(b, midnight + timedelta(minutes=we))
                if hi > lo:
                    total += (hi - lo).total_seconds() / 60.0
        day += timedelta(days=1)
    return total


def build_events(sources, policy):
    """构造扫描线事件并返回归一化后的在途单列表。

    事件元组：(时间, 类型, 增量, 载荷)；类型 0=营业时窗边界，1=在途单边界。
    同刻事件先处理时窗边界，保证半开区间 [起, 止) 的边界分钟归属确定。
    """
    zone = ZoneInfo(policy['timezone'])
    events = []
    cases = []
    lo = hi = None
    for idx, src in enumerate(sources):
        a = _parse(src['opened'], zone)
        b = _parse(src['closed'], zone)
        if b < a:
            raise ValueError('结束时间早于开始时间：%s' % src.get('natural_key', idx))
        cases.append({
            'idx': idx,
            'natural_key': src.get('natural_key', str(idx + 1)),
            'period': src['period'],
            'group': src['department'],
            'opened': src['opened'],
            'closed': src['closed'],
            'a': a,
            'b': b,
        })
        events.append((a, CASE_KIND, 1, idx))
        events.append((b, CASE_KIND, -1, idx))
        lo = a if lo is None or a < lo else lo
        hi = b if hi is None or b > hi else hi
    if cases:
        day = lo.date()
        while day <= hi.date():
            if day.weekday() in policy['weekdays'] and day.isoformat() not in policy.get('holidays', []):
                midnight = datetime.combine(day, time(), zone)
                for ws, we in policy['windows']:
                    events.append((midnight + timedelta(minutes=ws), WINDOW_KIND, 1, day))
                    events.append((midnight + timedelta(minutes=we), WINDOW_KIND, -1, day))
            day += timedelta(days=1)
    events.sort(key=lambda e: (e[0], e[1]))
    return events, cases, zone


def initial_state():
    """全新任务的扫描状态（结构可直接 JSON 序列化作为检查点）。"""
    return {
        'next_index': 0,
        'window_stack': [],          # 营业时窗栈，元素为业务日期 ISO 字符串
        'active': {},                # 在途单 idx -> 小组（中断续跑接回的跨段在途单）
        'group_counts': {},          # 小组 -> 当前在途单数
        'frac': {},                  # 日期 -> 小组 -> 累计精确分摊分钟
        'case_frac': {},             # 在途单 idx -> 累计精确分摊分钟
        'case_segments': {},         # 在途单 idx -> 参与的重叠段数
        'peak': {},                  # 日期|小组 -> 历史峰值在途单数
        'raw_total': 0.0,            # 去重前的全局精确分钟（并集口径）
        'segments_done': 0,
    }


def _state_from_checkpoint(checkpoint):
    # 深拷贝：续跑会原地累计 frac/group_counts 等嵌套结构，
    # 共享引用会污染检查点，导致同检查点重试时重复分摊。
    state = initial_state()
    state.update(copy.deepcopy(checkpoint))
    state['active'] = {int(k): v for k, v in state['active'].items()}
    state['case_frac'] = {int(k): v for k, v in state['case_frac'].items()}
    state['case_segments'] = {int(k): v for k, v in state['case_segments'].items()}
    return state


def _largest_remainder(weights, target):
    """最大余数法把整数 target 按 weights 比例分完，返回 {键: 整数}，总和恰为 target。

    余数比较保留 9 位小数消除浮点尾差；真同余时由 sorted 的键顺序决定
    （日期层传入 ISO 日期、小组层传入组名，均为升序），结果确定可复算。
    """
    floors = {k: math.floor(v) for k, v in weights.items()}
    leftover = target - sum(floors.values())
    def rem_key(k):
        rem = round(weights[k] - floors[k], 9)
        return (-rem, k)
    order = sorted(weights, key=rem_key)
    out = dict(floors)
    for k in order[:leftover]:
        out[k] += 1
    return out


def _rounding_allocation(state):
    """两级最大余数法：全局 -> 业务日期 -> 小组。"""
    frac = state['frac']
    date_weights = {d: sum(groups.values()) for d, groups in frac.items()}
    global_target = half_up(state['raw_total'])
    date_targets = _largest_remainder(date_weights, global_target)
    apportion = {}
    for d, groups in frac.items():
        group_targets = _largest_remainder(groups, date_targets[d])
        for g, minutes in group_targets.items():
            apportion[(d, g)] = minutes
    return global_target, date_targets, apportion


def occupancy_pipeline(sources, policy, checkpoint=None, on_checkpoint=None, chunk_size=5000):
    """执行去重分摊全流程。

    checkpoint: 续跑时传入上次检查点 state；None 表示从头开始。
    on_checkpoint(state, pending_segments): 每处理 chunk_size 个事件回调一次，
        由持久化层在同一事务落检查点与重叠段；回调内抛异常即中断，可原样续跑。
    返回 (result_occupancy, state)；调用方通过发布闸门后才可对外发布。
    """
    events, cases, zone = build_events(sources, policy)
    state = _state_from_checkpoint(checkpoint) if checkpoint else initial_state()
    pending_segments = []
    prev_t = events[state['next_index'] - 1][0] if state['next_index'] else None

    for i in range(state['next_index'], len(events)):
        t, kind, delta, payload = events[i]
        if prev_t is not None and t > prev_t and state['window_stack'] and state['active']:
            seconds = int(round((t - prev_t).total_seconds()))
            if seconds > 0:
                day = state['window_stack'][-1]
                n_total = len(state['active'])
                minutes = seconds / 60.0
                state['raw_total'] += minutes
                day_frac = state['frac'].setdefault(day, {})
                for group, count in state['group_counts'].items():
                    share = minutes * count / n_total
                    day_frac[group] = day_frac.get(group, 0.0) + share
                    peak_key = day + '|' + group
                    state['peak'][peak_key] = max(state['peak'].get(peak_key, 0), count)
                for idx in state['active']:
                    state['case_frac'][idx] = state['case_frac'].get(idx, 0.0) + minutes / n_total
                    state['case_segments'][idx] = state['case_segments'].get(idx, 0) + 1
                pending_segments.append({
                    'ordinal': state['segments_done'],
                    'start_at': prev_t.isoformat(),
                    'end_at': t.isoformat(),
                    'seconds': seconds,
                    'total_active': n_total,
                    'groups': dict(state['group_counts']),
                })
                state['segments_done'] += 1
        if kind == WINDOW_KIND:
            if delta == 1:
                state['window_stack'].append(payload.isoformat())
            else:
                if state['window_stack']:
                    state['window_stack'].pop()
        else:
            idx = payload
            group = cases[idx]['group']
            if delta == 1:
                state['active'][idx] = group
                state['group_counts'][group] = state['group_counts'].get(group, 0) + 1
            else:
                state['active'].pop(idx, None)
                left = state['group_counts'].get(group, 0) - 1
                if left <= 0:
                    state['group_counts'].pop(group, None)
                else:
                    state['group_counts'][group] = left
        prev_t = t
        processed = i + 1
        if on_checkpoint and processed % chunk_size == 0 and processed < len(events):
            state['next_index'] = processed
            on_checkpoint(state, pending_segments)
            pending_segments = []

    # 终态前强制 flush 尾部分段：小任务可能整轮都未达到 chunk_size，
    # 否则累积在内存的重叠段不会落盘，报告页将查不到任何分段。
    if on_checkpoint and pending_segments:
        state['next_index'] = len(events)
        on_checkpoint(state, pending_segments)
        pending_segments = []

    # 终态：游标到末尾、在途集合清空，检查点据此判断本run已完整扫描
    state['next_index'] = len(events)
    global_target, date_targets, apportion = _rounding_allocation(state)
    rows = []
    for (day, group) in sorted(apportion):
        exact = state['frac'][day].get(group, 0.0)
        rows.append({
            '日期': day,
            '小组': group,
            '占用分钟': apportion[(day, group)],
            '精确分钟': round(exact, 4),
            '在途峰值': state['peak'].get(day + '|' + group, 0),
        })
    date_rows = [
        {'日期': d, '占用分钟': date_targets[d], '精确分钟': round(sum(state['frac'][d].values()), 4)}
        for d in sorted(date_targets)
    ]
    zero_minute_cases = [c['natural_key'] for c in cases if state['case_frac'].get(c['idx'], 0.0) <= EPS]
    result = {
        'global_minutes': global_target,
        'raw_minutes': round(state['raw_total'], 4),
        'apportion': rows,
        'date_totals': date_rows,
        'case_count': len(cases),
        'segment_count': state['segments_done'],
        'timezone': policy['timezone'],
        'rounding': {
            'rule': '全局四舍五入后两级最大余数法：先分业务日期，再分小组；余数大者先补，同余按日期或组名升序',
            'chunk_size': chunk_size,
        },
        'method': (
            '冻结底稿与口径快照；在途区间与营业时窗统一转入口径时区，扫描线事件切分重叠段（不逐分钟展开）；'
            '每段去重分钟按当时各小组在途单数占比分摊；午休、假日、非工作日不落任何分段。'
            '全局分钟四舍五入（半数上进）后，以最大余数法先分日期再分小组，整数分钟守恒、可复算。'
        ),
        'warnings': [
            '去重占用为并集口径，逐单相加会重复计算重叠占用，本结果已按段去重。',
        ] + (['存在 %d 单在途区间与营业时窗无交集，未参与分摊：%s' % (len(zero_minute_cases), '、'.join(zero_minute_cases[:10]))] if zero_minute_cases else []),
    }
    validate_occupancy(result)
    return result, state


def validate_occupancy(occ):
    """发布闸门：全局值与分摊明细齐全且守恒，否则拒绝发布。"""
    required = ('global_minutes', 'apportion', 'date_totals', 'raw_minutes')
    if not isinstance(occ, dict) or any(k not in occ for k in required):
        raise ValueError('占用核算结果不完整，拒绝发布')
    if not occ['apportion']:
        # 全部在途单都落在非营业时段时，零分钟且零明细是合法且守恒的结果
        if occ['global_minutes'] != 0:
            raise ValueError('存在全局分钟但分摊明细为空，拒绝发布')
        return True
    if any(not isinstance(r['占用分钟'], int) or r['占用分钟'] < 0 for r in occ['apportion']):
        raise ValueError('分摊分钟必须为非负整数，拒绝发布')
    if sum(r['占用分钟'] for r in occ['apportion']) != occ['global_minutes']:
        raise ValueError('小组分摊之和与全局去重分钟不一致，拒绝发布')
    day_sum = {}
    for r in occ['apportion']:
        day_sum[r['日期']] = day_sum.get(r['日期'], 0) + r['占用分钟']
    for r in occ['date_totals']:
        if day_sum.get(r['日期']) != r['占用分钟']:
            raise ValueError('日期分摊守恒校验失败，拒绝发布')
    if sum(r['占用分钟'] for r in occ['date_totals']) != occ['global_minutes']:
        raise ValueError('日期分摊之和与全局去重分钟不一致，拒绝发布')
    return True


def case_share(segments, group, opened_iso, closed_iso):
    """依据重叠段明细计算单笔在途单的精确分摊分钟（逐笔追溯，按需调用）。

    segments: 按 ordinal 有序的字典行，含 start_at/end_at/total_active/groups。
    时间均为同一口径时区的 ISO 字符串，字典序与时间序一致，可用边界比较裁剪。
    """
    exact = 0.0
    touched = 0
    for seg in segments:
        if seg['end_at'] <= opened_iso or seg['start_at'] >= closed_iso:
            continue
        count = seg['groups'].get(group, 0)
        if count:
            exact += (seg['seconds'] / 60.0) * count / seg['total_active']
            touched += 1
    return exact, touched
