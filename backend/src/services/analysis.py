from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from itertools import product


ROSTER_FIELDS = ('age', 'registered', 'local_resident', 'needs_assistance')
MEAL_FIELDS = ('meal_id', 'meal_status')
EXIT = '退出 · 户籍在册但非常住'
ENTER_OLDER = '新增 · 65 岁及以上常住非本区户籍'
ENTER_NEED = '新增 · 60–64 岁常住且有助餐需求'
CHANGE_GROUPS = (EXIT, ENTER_OLDER, ENTER_NEED)
STATUS_ORDER = {'完成': 0, '试餐': 1, '撤销': 2}
MAX_COMPONENT_COMBINATIONS = 200_000


@dataclass(frozen=True)
class Resident:
    community: str
    age: int
    registered: bool
    local_resident: bool
    needs_assistance: bool

    @property
    def old(self):
        return self.registered and self.age >= 65

    @property
    def new(self):
        return self.local_resident and (self.age >= 65 or (self.age >= 60 and self.needs_assistance))

    @property
    def change(self):
        if self.old and not self.new:
            return EXIT
        if self.new and not self.old:
            return ENTER_OLDER if self.age >= 65 else ENTER_NEED
        return None


@dataclass(frozen=True)
class Meal:
    resident_id: str
    community: str
    day: date
    status: str


@dataclass(frozen=True)
class MealGroup:
    meal_id: str
    candidates: tuple
    duplicates: int
    raw_count: int


def text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label}不能为空且必须为文本')
    return value.strip()


def iso_day(value, label):
    try:
        value = text(value, label)
        day = date.fromisoformat(value)
        if day.isoformat() == value:
            return day
    except ValueError:
        pass
    raise ValueError(f'{label}应为 YYYY-MM-DD 格式的有效日期')


def integer(value, label, maximum):
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError:
            pass
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f'{label}必须是 0 到 {maximum} 的整数')
    return value


def require_blank(row, keys, label):
    if any(row.get(key) is not None and row[key] != '' for key in keys):
        raise ValueError(f'{label}字段应留空')


def ledger(rows):
    residents = {}
    records = defaultdict(list)
    snapshot = None
    for line, row in enumerate(rows, 2):
        try:
            kind = text(row.get('record_type'), '记录类型')
            resident_id = text(row.get('resident_id'), '居民编号')
            community = text(row.get('community'), '社区')
            day = iso_day(row.get('record_date'), '记录日期')
            if kind == '名单':
                require_blank(row, MEAL_FIELDS, '名单行的餐单')
                if resident_id in residents:
                    raise ValueError(f'名单中居民 {resident_id} 重复，请合并为一条快照')
                if snapshot is not None and day != snapshot:
                    raise ValueError(f'名单快照日期必须一致，既有日期为 {snapshot}')
                snapshot = day
                residents[resident_id] = Resident(
                    community, integer(row.get('age'), '年龄', 130),
                    bool(integer(row.get('registered'), '本区户籍在册', 1)),
                    bool(integer(row.get('local_resident'), '本区常住', 1)),
                    bool(integer(row.get('needs_assistance'), '助餐需求', 1)),
                )
            elif kind == '餐单':
                require_blank(row, ROSTER_FIELDS, '餐单行的名单')
                meal_id = text(row.get('meal_id'), '结算流水号')
                status = text(row.get('meal_status'), '结算状态')
                if status not in STATUS_ORDER:
                    raise ValueError('结算状态只接受完成、撤销或试餐')
                records[meal_id].append(Meal(resident_id, community, day, status))
            else:
                raise ValueError('记录类型只接受名单或餐单')
        except ValueError as error:
            raise ValueError(f'第 {line} 行：{error}') from None
    if snapshot is None:
        raise ValueError('批次至少需要一行居民名单')
    groups = {}
    duplicates = 0
    raw_total = 0
    for meal_id, items in records.items():
        candidates = tuple(dict.fromkeys(items))
        groups[meal_id] = MealGroup(meal_id, candidates, len(items) - len(candidates), len(items))
        duplicates += len(items) - len(candidates)
        raw_total += len(items)
    return residents, groups, snapshot, duplicates, raw_total


def validate_rows(rows):
    ledger(rows)


def period_settings(params):
    start = iso_day(params.get('period_start'), '统计起日')
    end = iso_day(params.get('period_end'), '统计止日')
    if start > end:
        raise ValueError('统计止日不得早于起日')
    try:
        threshold = Decimal(str(params.get('publish_threshold_pp')))
    except InvalidOperation:
        raise ValueError('发布门槛须为 0 到 100 之间的百分点') from None
    if not threshold.is_finite() or not 0 <= threshold <= 100:
        raise ValueError('发布门槛须为 0 到 100 之间的百分点')
    return start, end, threshold


def preference(meal):
    """Deterministic pick: a completed report first, then the earliest row."""
    return (STATUS_ORDER.get(meal.status, 9), meal.day, meal.community, meal.resident_id)


def canonical_choice(candidates):
    return min(candidates, key=preference)


def classify(meal, residents, start, end):
    if not start <= meal.day <= end:
        return '窗口之外', None
    if meal.status != '完成':
        return meal.status, None
    if meal.resident_id not in residents:
        return '居民不在名单', None
    return None, meal.resident_id


def evaluate(residents, groups, selection, start, end, duplicates, raw_total):
    days = defaultdict(list)
    rejected = Counter()
    for meal in selection.values():
        category, resident_id = classify(meal, residents, start, end)
        if category:
            rejected[category] += 1
        else:
            days[resident_id].append(meal.day)
    unselected = sum(len(group.candidates) - 1 for group in groups.values())
    return {
        'days': days,
        'audit': {
            '重复流水': duplicates,
            '同流水未采用': unselected,
            '窗口之外': rejected['窗口之外'],
            '撤销': rejected['撤销'],
            '试餐': rejected['试餐'],
            '居民不在名单': rejected['居民不在名单'],
            '重复用餐': sum(len(items) - 1 for items in days.values()),
            '名单内首笔完成': len(days),
        },
        'old_n': sum(1 for key in days if residents[key].old),
        'new_n': sum(1 for key in days if residents[key].new),
    }


def rate(numerator, denominator):
    return Fraction(100 * numerator, denominator) if denominator else None


def display(value):
    return round(float(value), 4) if value is not None else None


def table(title, columns, rows):
    return {'title': title, 'columns': columns, 'rows': rows}


def selection_rows(selection):
    return [{'meal_id': meal_id, 'resident_id': meal.resident_id, 'community': meal.community,
             'date': meal.day.isoformat(), 'status': meal.status}
            for meal_id, meal in sorted(selection.items())]


def pinned_selection(entries):
    """Reproduce an issued report exactly: the archived meal list is authoritative."""
    if not entries:
        raise ValueError('存档餐单为空，无法按当时餐单复算')
    selection = {}
    for entry in entries:
        meal_id = text(entry.get('meal_id'), '结算流水号')
        if meal_id in selection:
            raise ValueError(f'存档餐单的结算流水 {meal_id} 重复')
        meal = Meal(text(entry.get('resident_id'), '居民编号'), text(entry.get('community'), '社区'),
                    iso_day(entry.get('date'), '用餐日期'), text(entry.get('status'), '结算状态'))
        if meal.status not in STATUS_ORDER:
            raise ValueError(f'存档餐单的结算流水 {meal_id} 结算状态无效')
        selection[meal_id] = meal
    replay = {meal_id: MealGroup(meal_id, (meal,), 0, 1) for meal_id, meal in selection.items()}
    return selection, replay


def candidate_effect(meal, residents, start, end, fixed_served):
    """The resident this candidate would newly serve, or None when it changes no numerator."""
    _, resident_id = classify(meal, residents, start, end)
    return resident_id if resident_id is not None and resident_id not in fixed_served else None


def collision_components(collision_ids, effects):
    """Flow numbers that share a served resident must be resolved together; the rest are independent."""
    owner = {}
    parent = {meal_id: meal_id for meal_id in collision_ids}

    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for meal_id in collision_ids:
        for effect in effects[meal_id]:
            if effect is None:
                continue
            if effect in owner:
                left, right = find(owner[effect]), find(meal_id)
                if left != right:
                    parent[right] = left
            else:
                owner[effect] = meal_id
    grouped = defaultdict(list)
    for meal_id in collision_ids:
        grouped[find(meal_id)].append(meal_id)
    return [grouped[key] for key in grouped]


def enumerate_extremes(residents, groups, collision_ids, start, end, fixed_served, old_d, new_d):
    w_new, w_old = Fraction(100, new_d), Fraction(100, old_d)
    base_new = sum(1 for key in fixed_served if residents[key].new)
    base_old = sum(1 for key in fixed_served if residents[key].old)
    effects = {meal_id: [candidate_effect(meal, residents, start, end, fixed_served)
                         for meal in groups[meal_id].candidates] for meal_id in collision_ids}
    low_total = high_total = w_new * base_new - w_old * base_old
    low_count = high_count = 1
    low_picks, high_picks = {}, {}
    for component in collision_components(collision_ids, effects):
        options = [groups[meal_id].candidates for meal_id in component]
        combinations = 1
        for items in options:
            combinations *= len(items)
        if combinations > MAX_COMPONENT_COMBINATIONS:
            raise ValueError(
                f'同一结算流水的补报组合过多（{combinations} 种），超过 {MAX_COMPONENT_COMBINATIONS} 种上限，'
                '请先在源数据中合并冲突流水后重试')
        low = high = None
        low_signature = high_signature = None
        low_positions = high_positions = None
        low_hits = high_hits = 0
        for positions in product(*(range(len(items)) for items in options)):
            served = {effects[meal_id][position]
                      for meal_id, position in zip(component, positions, strict=True)} - {None}
            contribution = (w_new * sum(1 for key in served if residents[key].new)
                            - w_old * sum(1 for key in served if residents[key].old))
            signature = tuple(preference(options[offset][position])
                              for offset, position in enumerate(positions))
            if low is None or contribution < low:
                low, low_positions, low_signature, low_hits = contribution, positions, signature, 1
            elif contribution == low:
                low_hits += 1
                if signature < low_signature:
                    low_positions, low_signature = positions, signature
            if high is None or contribution > high:
                high, high_positions, high_signature, high_hits = contribution, positions, signature, 1
            elif contribution == high:
                high_hits += 1
                if signature > high_signature:
                    high_positions, high_signature = positions, signature
        low_total += low
        high_total += high
        low_count *= low_hits
        high_count *= high_hits
        for offset, meal_id in enumerate(component):
            low_picks[meal_id] = options[offset][low_positions[offset]]
            high_picks[meal_id] = options[offset][high_positions[offset]]
    return {'low': low_total, 'high': high_total, 'low_picks': low_picks,
            'high_picks': high_picks, 'low_count': low_count, 'high_count': high_count}


def analyze(rows, params, pinned=None):
    start, end, threshold = period_settings(params)
    residents, groups, snapshot, duplicate_meals, raw_total = ledger(rows)
    by_community = defaultdict(list)
    for key, resident in residents.items():
        by_community[resident.community].append(key)
    old_d = sum(1 for resident in residents.values() if resident.old)
    new_d = sum(1 for resident in residents.values() if resident.new)
    collision_ids = sorted(meal_id for meal_id, group in groups.items() if len(group.candidates) > 1)
    combinations = 1
    for meal_id in collision_ids:
        combinations *= len(groups[meal_id].candidates)

    if pinned is not None:
        reference, groups = pinned_selection(pinned)
        collision_ids = []
        combinations = 1
        # The archived meal list is the source of truth; source-side duplicates no longer apply.
        duplicate_meals, raw_total = 0, len(reference)
    else:
        reference = {meal_id: canonical_choice(group.candidates) for meal_id, group in groups.items()}

    reference_result = evaluate(residents, groups, reference, start, end, duplicate_meals, raw_total)
    comparable = old_d > 0 and new_d > 0
    old_pct, new_pct = rate(reference_result['old_n'], old_d), rate(reference_result['new_n'], new_d)
    reference_delta = new_pct - old_pct if comparable else None

    fixed_ids = [meal_id for meal_id in groups if meal_id not in collision_ids]
    fixed_served = set()
    for meal_id in fixed_ids:
        _, resident_id = classify(reference[meal_id], residents, start, end)
        if resident_id is not None:
            fixed_served.add(resident_id)
    fixed_selection = {meal_id: reference[meal_id] for meal_id in fixed_ids}

    if pinned is not None or not collision_ids or not comparable:
        low = high = reference_delta
        low_selection = high_selection = reference
        low_count = high_count = combinations
    else:
        extremes = enumerate_extremes(residents, groups, collision_ids, start, end, fixed_served, old_d, new_d)
        low, high = extremes['low'], extremes['high']
        low_selection = {**fixed_selection, **extremes['low_picks']}
        high_selection = {**fixed_selection, **extremes['high_picks']}
        low_count, high_count = extremes['low_count'], extremes['high_count']

    ready = bool(comparable and low >= -Fraction(threshold) and high <= Fraction(threshold))
    threshold_label = str(int(threshold)) if threshold == threshold.to_integral_value() else str(threshold)
    if not comparable:
        summary = (f'暂缓发布。旧版覆盖 {reference_result["old_n"]}/{old_d} 人，'
                   f'新版覆盖 {reference_result["new_n"]}/{new_d} 人，至少一版分母为零。')
    elif not collision_ids and pinned is None:
        action = '可发布' if ready else '暂缓发布'
        summary = (f'{action}。旧版 {reference_result["old_n"]}/{old_d}（{display(old_pct):.4f}%），'
                   f'新版 {reference_result["new_n"]}/{new_d}（{display(new_pct):.4f}%）。'
                   f'新版变化 {display(reference_delta):+.4f} 个百分点，发布门槛为 {threshold_label} 个百分点。')
    else:
        lead = '按已发月报存档的餐单复算。' if pinned is not None else ''
        if pinned is not None:
            scope = '按存档餐单组合复算，结果不随源数据更新变化。'
            scope_detail = f'存档组合换版差值 {display(reference_delta):+.4f} 个百分点。'
        else:
            scope = '整个可行范围均在门槛内。' if ready else '可行范围超出门槛，暂缓分发。'
            scope_detail = (f'同流水补报 {len(collision_ids)} 组，共 {combinations} 种可行组合，'
                            f'换版差值范围 {display(low):+.4f} 至 {display(high):+.4f} 个百分点。')
        action = '可发布' if ready else '暂缓发布'
        summary = (f'{lead}{action}。旧版 {reference_result["old_n"]}/{old_d}（{display(old_pct):.4f}%），'
                   f'新版 {reference_result["new_n"]}/{new_d}（{display(new_pct):.4f}%）。'
                   f'{scope_detail}发布门槛为 {threshold_label} 个百分点。{scope}')

    community_rows = []
    for name, keys in sorted(by_community.items()):
        before_d = sum(1 for key in keys if residents[key].old)
        after_d = sum(1 for key in keys if residents[key].new)
        before_n = sum(1 for key in keys if residents[key].old and key in reference_result['days'])
        after_n = sum(1 for key in keys if residents[key].new and key in reference_result['days'])
        before, after = rate(before_n, before_d), rate(after_n, after_d)
        community_rows.append([name, before_n, before_d, display(before), after_n, after_d, display(after),
                               display(after - before) if after is not None and before is not None else None])

    change_counts = Counter()
    change_served = Counter()
    details = []
    for key, resident in sorted(residents.items(), key=lambda item: (item[1].community, item[0])):
        group = resident.change
        if group is None:
            continue
        change_counts[group] += 1
        days = reference_result['days'].get(key, [])
        if days:
            change_served[group] += 1
        details.append([resident.community, key, resident.age, group, len(days),
                        min(days).isoformat() if days else None])

    bridge = []
    for group in CHANGE_GROUPS:
        sign = -1 if group == EXIT else 1
        dn, dd = sign * change_served[group], sign * change_counts[group]
        contribution = (100 * dn - old_pct * dd) / new_d if comparable else None
        bridge.append([group, change_counts[group], change_served[group], dn, dd, display(contribution)])
    bridge.append(['合计变化', sum(change_counts.values()), sum(change_served.values()),
                   reference_result['new_n'] - reference_result['old_n'], new_d - old_d,
                   display(reference_delta)])

    exclusions = [
        ['旧版分子', len(reference_result['days']), len(reference_result['days']) - reference_result['old_n'],
         reference_result['old_n']],
        ['新版分子', len(reference_result['days']), len(reference_result['days']) - reference_result['new_n'],
         reference_result['new_n']],
    ]

    tables = [table('分社区覆盖率', ['名单归属社区', '旧版分子', '旧版分母', '旧版率（%）', '新版分子', '新版分母',
                                     '新版率（%）', '变化（百分点）'],
                    [['全区加权', reference_result['old_n'], old_d, display(old_pct), reference_result['new_n'],
                      new_d, display(new_pct), display(reference_delta)], *community_rows])]
    if collision_ids and pinned is None and comparable:
        low_result = evaluate(residents, groups, low_selection, start, end, duplicate_meals, raw_total)
        high_result = evaluate(residents, groups, high_selection, start, end, duplicate_meals, raw_total)
        tables.append(table('换版差值可行范围', ['组合端', '旧版分子', '旧版分母', '新版分子', '新版分母',
                                                 '换版差值（百分点）', '达到组合数'],
                            [['最小差值', low_result['old_n'], old_d, low_result['new_n'], new_d, display(low),
                              low_count],
                             ['最大差值', high_result['old_n'], old_d, high_result['new_n'], new_d, display(high),
                              high_count],
                             ['参考组合', reference_result['old_n'], old_d, reference_result['new_n'], new_d,
                              display(reference_delta), 1]]))
        extreme_rows = []
        for label, selection in (('最小差值', low_selection), ('最大差值', high_selection)):
            for meal_id in collision_ids:
                meal = selection[meal_id]
                extreme_rows.append([label, meal_id, meal.resident_id, meal.community,
                                     meal.day.isoformat(), meal.status])
        tables.append(table('两端所选补报餐单', ['差值端', '结算流水号', '居民编号', '用餐社区', '用餐日期', '结算状态'],
                            extreme_rows))
        tables.append(table('补报流水冲突', ['结算流水号', '候选条数', '重复上报', '参考选取'],
                            [[meal_id, len(groups[meal_id].candidates), groups[meal_id].duplicates,
                              canonical_choice(groups[meal_id].candidates).status] for meal_id in collision_ids]))
    tables.extend([
        table('换版归因桥', ['资格变动', '涉及居民', '其中已用餐', '分子增减', '分母增减', '贡献（百分点）'], bridge),
        table('资格变动居民明细', ['名单归属社区', '居民编号', '年龄', '资格变动', '期内完成餐单', '首笔完成日期'], details),
        table('餐单逐级核销', ['核销去向', '餐单条数'],
              [['原始餐单', raw_total], *[[label, number] for label, number in reference_result['audit'].items()]]),
        table('两版分子入账', ['口径', '名单内去重后用餐人数', '因版本资格排除', '计入分子'], exclusions),
    ])

    notes = [
        f'统计窗口 {start} 至 {end}，含首尾两日。居民资格固定为 {snapshot} 的名单快照。',
        '跨食堂用餐按居民去重，并归入名单所属社区。冻结名单和期内用餐分别记录资格与事实。',
        '分母为零时覆盖率留空。各社区分母作为权重，归因贡献与发布门槛均使用未舍入比例。',
    ]
    if collision_ids:
        if pinned is not None:
            notes.append('本次按已发月报存档的餐单组合复算，源数据后续更新不影响该结果。')
        else:
            notes.append(f'同一结算流水存在 {len(collision_ids)} 组补报冲突，每组只选一条实报，两版共用同一组餐单；'
                         f'参考组合按完成优先、日期最早选取，已遍历全部 {combinations} 种可行组合。')
            notes.append('最小与最大差值两端所选餐单见“两端所选补报餐单”，分发要求整个范围都在门槛内。')

    extremes_view = None
    if collision_ids and pinned is None and comparable:
        extremes_view = {
            'min': {'delta': display(low), 'combinations': low_count,
                    'meals': [{'meal_id': meal_id, 'resident_id': low_selection[meal_id].resident_id,
                               'community': low_selection[meal_id].community,
                               'date': low_selection[meal_id].day.isoformat(),
                               'status': low_selection[meal_id].status} for meal_id in collision_ids]},
            'max': {'delta': display(high), 'combinations': high_count,
                    'meals': [{'meal_id': meal_id, 'resident_id': high_selection[meal_id].resident_id,
                               'community': high_selection[meal_id].community,
                               'date': high_selection[meal_id].day.isoformat(),
                               'status': high_selection[meal_id].status} for meal_id in collision_ids]},
        }
    return {
        'summary': summary,
        'ready': ready,
        'metrics': [
            {'label': '旧版区级覆盖率（%）', 'value': display(old_pct),
             'detail': f'{reference_result["old_n"]} 名用餐居民 / {old_d} 名户籍老人'},
            {'label': '新版区级覆盖率（%）', 'value': display(new_pct),
             'detail': f'{reference_result["new_n"]} 名用餐居民 / {new_d} 名符合条件的常住居民'},
            {'label': '换版差值范围（百分点）',
             'value': (f'{display(low):+.4f} ~ {display(high):+.4f}'
                       if comparable and low != high else display(reference_delta)),
             'detail': (f'新版减旧版 · {combinations} 种可行组合 · 门槛 {threshold_label} 个百分点'
                        if collision_ids else f'新版减旧版 · 门槛 {threshold_label} 个百分点')},
            {'label': '月报分发', 'value': '可发布' if ready else '暂缓发布',
             'detail': '整个可行范围都在门槛内（含等于）才可分发'},
        ],
        'tables': tables,
        'notes': notes,
        'series_title': '全区加权覆盖率（%）',
        'series': [{'label': label, 'value': display(value)}
                   for label, value in [('旧版', old_pct), ('新版', new_pct)] if value is not None],
        'selection': selection_rows(reference),
        'extremes': extremes_view,
        'range': {'min': display(low), 'max': display(high), 'combinations': combinations,
                  'collisions': len(collision_ids), 'min_combinations': low_count,
                  'max_combinations': high_count, 'pinned': pinned is not None},
    }
