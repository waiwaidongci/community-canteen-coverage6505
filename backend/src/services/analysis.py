from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from fractions import Fraction


ROSTER_FIELDS = ('age', 'registered', 'local_resident', 'needs_assistance')
MEAL_FIELDS = ('meal_id', 'meal_status')
EXIT = '退出 · 户籍在册但非常住'
ENTER_OLDER = '新增 · 65 岁及以上常住非本区户籍'
ENTER_NEED = '新增 · 60–64 岁常住且有助餐需求'
CHANGE_GROUPS = (EXIT, ENTER_OLDER, ENTER_NEED)


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
    meals = {}
    snapshot = None
    duplicate_meals = 0
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
                if status not in {'完成', '撤销', '试餐'}:
                    raise ValueError('结算状态只接受完成、撤销或试餐')
                meal = Meal(resident_id, community, day, status)
                previous = meals.get(meal_id)
                if previous is None:
                    meals[meal_id] = meal
                elif previous == meal:
                    duplicate_meals += 1
                else:
                    raise ValueError(f'结算流水 {meal_id} 重复且内容冲突，请先核对源单')
            else:
                raise ValueError('记录类型只接受名单或餐单')
        except ValueError as error:
            raise ValueError(f'第 {line} 行：{error}') from None
    if snapshot is None:
        raise ValueError('批次至少需要一行居民名单')
    return residents, meals, snapshot, duplicate_meals


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


def attendance(residents, meals, duplicate_meals, start, end):
    valid = defaultdict(list)
    counts = dict.fromkeys(('重复流水', '窗口之外', '撤销', '试餐', '居民不在名单', '重复用餐', '名单内首笔完成'), 0)
    counts['重复流水'] = duplicate_meals
    for meal in meals.values():
        if not start <= meal.day <= end:
            counts['窗口之外'] += 1
        elif meal.status != '完成':
            counts[meal.status] += 1
        elif meal.resident_id not in residents:
            counts['居民不在名单'] += 1
        else:
            valid[meal.resident_id].append(meal.day)
    counts['名单内首笔完成'] = len(valid)
    counts['重复用餐'] = sum(len(days) - 1 for days in valid.values())
    return valid, counts


def coverage(resident_ids, residents, visits):
    old = {key for key in resident_ids if residents[key].old}
    new = {key for key in resident_ids if residents[key].new}
    served = visits.keys()
    return len(old & served), len(old), len(new & served), len(new)


def rate(numerator, denominator):
    return Fraction(100 * numerator, denominator) if denominator else None


def display(value):
    return round(float(value), 4) if value is not None else None


def table(title, columns, rows):
    return {'title': title, 'columns': columns, 'rows': rows}


def analyze(rows, params):
    start, end, threshold = period_settings(params)
    residents, meals, snapshot, duplicate_meals = ledger(rows)
    visits, audit = attendance(residents, meals, duplicate_meals, start, end)
    by_community = defaultdict(list)
    for key, resident in residents.items():
        by_community[resident.community].append(key)

    old_n, old_d, new_n, new_d = coverage(residents, residents, visits)
    old_pct, new_pct = rate(old_n, old_d), rate(new_n, new_d)
    comparable = old_pct is not None and new_pct is not None
    delta = new_pct - old_pct if comparable else None
    ready = comparable and abs(delta) <= Fraction(threshold)
    threshold_label = str(int(threshold)) if threshold == threshold.to_integral_value() else str(threshold)
    if not comparable:
        summary = f'暂缓发布。旧版覆盖 {old_n}/{old_d} 人，新版覆盖 {new_n}/{new_d} 人，至少一版分母为零。'
    else:
        action = '可发布' if ready else '暂缓发布'
        summary = (
            f'{action}。旧版 {old_n}/{old_d}（{display(old_pct):.4f}%），新版 {new_n}/{new_d}（{display(new_pct):.4f}%）。'
            f'新版变化 {display(delta):+.4f} 个百分点，发布门槛为 {threshold_label} 个百分点。'
        )

    community_rows = []
    for name, keys in sorted(by_community.items()):
        before_n, before_d, after_n, after_d = coverage(keys, residents, visits)
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
        days = visits.get(key, [])
        if days:
            change_served[group] += 1
        details.append([resident.community, key, resident.age, group, len(days), min(days).isoformat() if days else None])

    bridge = []
    for group in CHANGE_GROUPS:
        sign = -1 if group == EXIT else 1
        dn, dd = sign * change_served[group], sign * change_counts[group]
        contribution = (100 * dn - old_pct * dd) / new_d if comparable else None
        bridge.append([group, change_counts[group], change_served[group], dn, dd, display(contribution)])
    bridge.append(['合计变化', sum(change_counts.values()), sum(change_served.values()),
                   new_n - old_n, new_d - old_d, display(delta)])

    exclusions = [
        ['旧版分子', len(visits), len(visits) - old_n, old_n],
        ['新版分子', len(visits), len(visits) - new_n, new_n],
    ]
    return {
        'summary': summary,
        'ready': ready,
        'metrics': [
            {'label': '旧版区级覆盖率（%）', 'value': display(old_pct), 'detail': f'{old_n} 名用餐居民 / {old_d} 名户籍老人'},
            {'label': '新版区级覆盖率（%）', 'value': display(new_pct), 'detail': f'{new_n} 名用餐居民 / {new_d} 名符合条件的常住居民'},
            {'label': '换版差值（百分点）', 'value': display(delta), 'detail': f'新版减旧版 · 发布门槛 {threshold_label} 个百分点'},
            {'label': '月报分发', 'value': '可发布' if ready else '暂缓发布', 'detail': '使用未舍入差值判定，等于门槛时可发布'},
        ],
        'tables': [
            table('分社区覆盖率', ['名单归属社区', '旧版分子', '旧版分母', '旧版率（%）', '新版分子', '新版分母',
                                     '新版率（%）', '变化（百分点）'],
                  [['全区加权', old_n, old_d, display(old_pct), new_n, new_d, display(new_pct), display(delta)],
                   *community_rows]),
            table('换版归因桥', ['资格变动', '涉及居民', '其中已用餐', '分子增减', '分母增减', '贡献（百分点）'], bridge),
            table('资格变动居民明细', ['名单归属社区', '居民编号', '年龄', '资格变动', '期内完成餐单', '首笔完成日期'], details),
            table('餐单逐级核销', ['核销去向', '餐单条数'], [['原始餐单', len(meals) + duplicate_meals],
                                                    *[[label, number] for label, number in audit.items()]]),
            table('两版分子入账', ['口径', '名单内去重后用餐人数', '因版本资格排除', '计入分子'], exclusions),
        ],
        'notes': [
            f'统计窗口 {start} 至 {end}，含首尾两日。居民资格固定为 {snapshot} 的名单快照。',
            '跨食堂用餐按居民去重，并归入名单所属社区。冻结名单和期内用餐分别记录资格与事实。',
            '分母为零时覆盖率留空。各社区分母作为权重，归因贡献与发布门槛均使用未舍入比例。',
        ],
        'series_title': '全区加权覆盖率（%）',
        'series': [{'label': label, 'value': display(value)} for label, value in [('旧版', old_pct), ('新版', new_pct)]
                   if value is not None],
    }
