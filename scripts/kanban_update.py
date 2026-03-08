#!/usr/bin/env python3
"""
看板任务更新工具 - 供各省部 Agent 调用

用法:
  # 新建任务（收旨时）
  python3 kanban_update.py create JJC-20260223-012 "任务标题" Zhongshu 中书省 中书令

  # 自动分配当日下一个 JJC 编号并创建任务（推荐）
  python3 kanban_update.py create_auto "任务标题" Zhongshu 中书省 中书令 "太子整理旨意"

  # 更新状态
  python3 kanban_update.py state JJC-20260223-012 Menxia "规划方案已提交门下省"

  # 添加流转记录
  python3 kanban_update.py flow JJC-20260223-012 "中书省" "门下省" "规划方案提交审核"

  # 完成任务
  python3 kanban_update.py done JJC-20260223-012 "/path/to/output" "任务完成摘要"

  # 添加/更新子任务 todo
  python3 kanban_update.py todo JJC-20260223-012 1 "实现API接口" in-progress
  python3 kanban_update.py todo JJC-20260223-012 1 "" completed

  # 🔥 实时进展汇报（Agent 主动调用，频率不限）
  python3 kanban_update.py progress JJC-20260223-012 "正在分析需求，拟定3个子方案" "1.调研技术选型|2.撰写设计文档|3.实现原型"
"""
import json, pathlib, datetime, sys, subprocess, logging, os, re

_SCRIPT_BASE = pathlib.Path(__file__).resolve().parent.parent

def _resolve_dashboard_base():
    candidates = []
    for env_name in ('OPENCLAW_EDICT_HOME', 'OPENCLAW_DASHBOARD_HOME'):
        raw = (os.environ.get(env_name) or '').strip()
        if raw:
            candidates.append(pathlib.Path(raw).expanduser())

    home = pathlib.Path.home() / '.openclaw'
    candidates.extend([
        home / 'workspace' / 'edict',
        home / 'workspace-main',
        _SCRIPT_BASE,
    ])

    seen = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            resolved = candidate
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        if (resolved / 'data').is_dir() and (resolved / 'scripts' / 'refresh_live_data.py').exists():
            return resolved

    return _SCRIPT_BASE

_BASE = _resolve_dashboard_base()
TASKS_FILE = _BASE / 'data' / 'tasks_source.json'
REFRESH_SCRIPT = _BASE / 'scripts' / 'refresh_live_data.py'

log = logging.getLogger('kanban')
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(message)s', datefmt='%H:%M:%S')

# 文件锁 —— 防止多 Agent 同时读写 tasks_source.json
from file_lock import atomic_json_read, atomic_json_update, atomic_json_write  # noqa: E402

try:
    import shiguan_hooks  # noqa: E402
except Exception:
    shiguan_hooks = None

STATE_ORG_MAP = {
    'Taizi': '太子', 'Zhongshu': '中书省', 'Menxia': '门下省', 'Assigned': '尚书省',
    'Doing': '执行中', 'Review': '尚书省', 'Done': '完成', 'Blocked': '阻塞',
}

_STATE_AGENT_MAP = {
    'Taizi': 'main',
    'Zhongshu': 'zhongshu',
    'Menxia': 'menxia',
    'Assigned': 'shangshu',
    'Review': 'shangshu',
    'Pending': 'zhongshu',
}

_ORG_AGENT_MAP = {
    '礼部': 'libu', '户部': 'hubu', '兵部': 'bingbu',
    '刑部': 'xingbu', '工部': 'gongbu', '吏部': 'libu_hr',
    '中书省': 'zhongshu', '门下省': 'menxia', '尚书省': 'shangshu',
}

_AGENT_LABELS = {
    'main': '太子', 'taizi': '太子',
    'zhongshu': '中书省', 'menxia': '门下省', 'shangshu': '尚书省',
    'libu': '礼部', 'hubu': '户部', 'bingbu': '兵部', 'xingbu': '刑部',
    'gongbu': '工部', 'libu_hr': '吏部', 'zaochao': '钦天监',
}

_EXECUTION_AGENT_IDS = {'shangshu', 'libu', 'hubu', 'bingbu', 'xingbu', 'gongbu', 'libu_hr'}
_EXECUTION_DEPTS = {'尚书省', '礼部', '户部', '兵部', '刑部', '工部', '吏部'}
_FLOW_STATE_TRANSITIONS = {
    ('太子', '中书省'): ('Zhongshu', '中书省'),
    ('中书省', '门下省'): ('Menxia', '门下省'),
    ('门下省', '中书省'): ('Zhongshu', '中书省'),
    ('门下省', '尚书省'): ('Assigned', '尚书省'),
}

MAX_PROGRESS_LOG = 100  # 单任务最大进展日志条数

def load():
    return atomic_json_read(TASKS_FILE, [])

def save(tasks):
    atomic_json_write(TASKS_FILE, tasks)
    # 异步触发刷新，不阻塞调用方
    try:
        subprocess.Popen(['python3', str(REFRESH_SCRIPT)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00', 'Z')

def find_task(tasks, task_id):
    return next((t for t in tasks if t.get('id') == task_id), None)


# 旨意标题最低要求
_MIN_TITLE_LEN = 6
_JUNK_TITLES = {
    '?', '？', '好', '好的', '是', '否', '不', '不是', '对', '了解', '收到',
    '嗯', '哦', '知道了', '开启了么', '可以', '不行', '行', 'ok', 'yes', 'no',
    '你去开启', '测试', '试试', '看看',
}
_TASK_ID_RE = re.compile(r'^JJC-(\d{8})-(\d{3})$')
_CHINA_TZ = datetime.timezone(datetime.timedelta(hours=8))


class KanbanInputError(ValueError):
    """输入参数校验失败。"""


def _china_today_yyyymmdd():
    return datetime.datetime.now(_CHINA_TZ).strftime('%Y%m%d')


def _validate_new_task_id(task_id):
    """校验新建任务 ID，默认要求日期必须是北京时间今天。"""
    if not task_id:
        raise KanbanInputError('任务ID为空')
    match = _TASK_ID_RE.fullmatch(task_id)
    if not match:
        raise KanbanInputError(f'任务ID格式非法: {task_id}（应为 JJC-YYYYMMDD-NNN）')
    if os.environ.get('KANBAN_ALLOW_NON_TODAY_ID') == '1':
        return
    task_day = match.group(1)
    today_day = _china_today_yyyymmdd()
    if task_day != today_day:
        raise KanbanInputError(
            f'任务ID日期与今日不一致: {task_day} ≠ {today_day}（如需回填请设置 KANBAN_ALLOW_NON_TODAY_ID=1）'
        )


def _next_jjc_task_id(tasks, day=None):
    """基于现有 tasks 原子计算当日下一个 JJC 编号。"""
    task_day = day or _china_today_yyyymmdd()
    max_seq = 0
    for task in tasks or []:
        task_id = str((task or {}).get('id', '')).strip()
        match = _TASK_ID_RE.fullmatch(task_id)
        if not match or match.group(1) != task_day:
            continue
        try:
            max_seq = max(max_seq, int(match.group(2)))
        except Exception:
            continue
    return f'JJC-{task_day}-{max_seq + 1:03d}'

def _sanitize_text(raw, max_len=80):
    """清洗文本：剥离文件路径、URL、Conversation 元数据、传旨前缀、截断过长内容。"""
    t = (raw or '').strip()
    # 1) 剥离 Conversation info / Conversation 后面的所有内容
    t = re.split(r'\n*Conversation\b', t, maxsplit=1)[0].strip()
    # 2) 剥离 ```json 代码块
    t = re.split(r'\n*```', t, maxsplit=1)[0].strip()
    # 3) 剥离 Unix/Mac 文件路径 (/Users/xxx, /home/xxx, /opt/xxx, ./xxx)
    t = re.sub(r'[/\\.~][A-Za-z0-9_\-./]+(?:\.(?:py|js|ts|json|md|sh|yaml|yml|txt|csv|html|css|log))?', '', t)
    # 4) 剥离 URL
    t = re.sub(r'https?://\S+', '', t)
    # 5) 清理常见前缀: "传旨:" "下旨:" "下旨（xxx）:" 等
    t = re.sub(r'^(传旨|下旨)([（(][^)）]*[)）])?[：:\uff1a]\s*', '', t)
    # 6) 剥离系统元数据关键词
    t = re.sub(r'(message_id|session_id|chat_id|open_id|user_id|tenant_key)\s*[:=]\s*\S+', '', t)
    # 7) 合并多余空白
    t = re.sub(r'\s+', ' ', t).strip()
    # 8) 截断过长内容
    if len(t) > max_len:
        t = t[:max_len] + '…'
    return t


def _sanitize_title(raw):
    """清洗标题（最长 80 字符）。"""
    return _sanitize_text(raw, 80)


def _sanitize_remark(raw):
    """清洗流转备注（最长 120 字符）。"""
    return _sanitize_text(raw, 120)


def _infer_agent_id_from_runtime(task=None):
    """尽量推断当前执行该命令的 Agent。"""
    for k in ('OPENCLAW_AGENT_ID', 'OPENCLAW_AGENT', 'AGENT_ID'):
        v = (os.environ.get(k) or '').strip()
        if v:
            return v

    cwd = str(pathlib.Path.cwd())
    m = re.search(r'workspace-([a-zA-Z0-9_\-]+)', cwd)
    if m:
        return m.group(1)

    fpath = str(pathlib.Path(__file__).resolve())
    m2 = re.search(r'workspace-([a-zA-Z0-9_\-]+)', fpath)
    if m2:
        return m2.group(1)

    if task:
        state = task.get('state', '')
        org = task.get('org', '')
        aid = _STATE_AGENT_MAP.get(state)
        if aid is None and state in ('Doing', 'Next'):
            aid = _ORG_AGENT_MAP.get(org)
        if aid:
            return aid
    return ''


def _is_jjc_task(task):
    return str((task or {}).get('id', '')).startswith('JJC-')


def _has_execution_handoff(task):
    if not task:
        return False
    if task.get('state') in ('Assigned', 'Review', 'Doing'):
        return True
    if task.get('org') in _EXECUTION_DEPTS:
        return True

    sched = task.get('_scheduler') or {}
    if sched.get('lastDispatchAgent') in _EXECUTION_AGENT_IDS:
        return True

    for entry in task.get('flow_log', []) or []:
        from_dept = entry.get('from', '')
        to_dept = entry.get('to', '')
        if from_dept in _EXECUTION_DEPTS or to_dept in _EXECUTION_DEPTS:
            return True

    for entry in task.get('progress_log', []) or []:
        if entry.get('agent') in _EXECUTION_AGENT_IDS:
            return True
        if entry.get('org') in _EXECUTION_DEPTS:
            return True

    return False


def _validate_terminal_transition(task, actor_agent_id, target_state='Done'):
    if target_state not in ('Done', 'Cancelled'):
        return
    if not _is_jjc_task(task):
        return

    actor = (actor_agent_id or '').strip().lower()
    if actor == 'menxia':
        raise KanbanInputError('门下省不得将 JJC 任务直接标记完成；请给出审议意见或转尚书省执行')

    if actor in _EXECUTION_AGENT_IDS:
        return

    if _has_execution_handoff(task):
        return

    raise KanbanInputError('JJC 任务尚未经过尚书省/六部执行链路，禁止直接标记完成')


def _ensure_mutable_jjc_task(task, operation, target_state=None):
    """禁止对已终态的 JJC 任务继续做普通写操作。"""
    if not _is_jjc_task(task):
        return
    current_state = str((task or {}).get('state', '')).strip()
    if current_state not in ('Done', 'Cancelled'):
        return
    if operation == 'state' and target_state == current_state:
        return
    raise KanbanInputError(
        f'JJC 任务已处于终态（{current_state}），禁止通过 {operation} 重新打开或追加变更；请创建新任务ID'
    )


def _touch_scheduler_progress(task):
    sched = _ensure_scheduler(task)
    ts = now_iso()
    sched['lastProgressAt'] = ts
    sched['stallSince'] = None
    sched['retryCount'] = 0
    sched['escalationLevel'] = 0
    sched['lastEscalatedAt'] = None


def _ensure_scheduler(task):
    sched = task.setdefault('_scheduler', {})
    if not isinstance(sched, dict):
        sched = {}
        task['_scheduler'] = sched
    sched.setdefault('enabled', True)
    sched.setdefault('stallThresholdSec', 180)
    sched.setdefault('maxRetry', 1)
    sched.setdefault('retryCount', 0)
    sched.setdefault('escalationLevel', 0)
    sched.setdefault('autoRollback', True)
    sched.setdefault('lastProgressAt', task.get('updatedAt') or now_iso())
    sched.setdefault('stallSince', None)
    sched.setdefault('lastDispatchStatus', 'idle')
    sched.setdefault('snapshot', {
        'state': task.get('state', ''),
        'org': task.get('org', ''),
        'now': task.get('now', ''),
        'savedAt': now_iso(),
        'note': 'init',
    })
    return sched


def _scheduler_snapshot(task, note=''):
    sched = _ensure_scheduler(task)
    sched['snapshot'] = {
        'state': task.get('state', ''),
        'org': task.get('org', ''),
        'now': task.get('now', ''),
        'savedAt': now_iso(),
        'note': note or 'snapshot',
    }


def _resolve_agent_for_task_state(state, org=''):
    aid = _STATE_AGENT_MAP.get(state)
    if aid is None and state in ('Doing', 'Next'):
        aid = _ORG_AGENT_MAP.get(org)
    return aid or ''


def _mark_scheduler_dispatch(task, trigger='kanban-flow'):
    sched = _ensure_scheduler(task)
    agent_id = _resolve_agent_for_task_state(task.get('state', ''), task.get('org', ''))
    if not agent_id:
        return
    sched['lastDispatchAt'] = now_iso()
    sched['lastDispatchStatus'] = 'success'
    sched['lastDispatchAgent'] = agent_id
    sched['lastDispatchTrigger'] = trigger
    sched['lastDispatchError'] = ''


def _apply_flow_state_transition(task, from_dept, to_dept):
    if not _is_jjc_task(task):
        return
    if task.get('state') in ('Done', 'Cancelled'):
        return

    next_state = None
    next_org = None
    direct = _FLOW_STATE_TRANSITIONS.get((from_dept, to_dept))
    if direct:
        next_state, next_org = direct
    elif from_dept == '尚书省' and to_dept in _EXECUTION_DEPTS - {'尚书省'}:
        next_state, next_org = 'Doing', to_dept
    elif to_dept == '尚书省' and (from_dept in _EXECUTION_DEPTS - {'尚书省'} or from_dept == '六部'):
        next_state, next_org = 'Review', '尚书省'

    if not next_state:
        return False
    task['state'] = next_state
    task['org'] = next_org
    return True


def _is_valid_task_title(title):
    """校验标题是否足够作为一个旨意任务。"""
    t = (title or '').strip()
    if len(t) < _MIN_TITLE_LEN:
        return False, f'标题过短（{len(t)}<{_MIN_TITLE_LEN}字），疑似非旨意'
    if t.lower() in _JUNK_TITLES:
        return False, f'标题 "{t}" 不是有效旨意'
    # 纯标点或问号
    if re.fullmatch(r'[\s?？!！.。,，…·\-—~]+', t):
        return False, '标题只有标点符号'
    # 看起来像文件路径
    if re.match(r'^[/\\~.]', t) or re.search(r'/[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+', t):
        return False, f'标题看起来像文件路径，请用中文概括任务'
    # 只剩标点和空白（清洗后可能变空）
    if re.fullmatch(r'[\s\W]*', t):
        return False, '标题清洗后为空'
    return True, ''


def cmd_create(task_id, title, state, org, official, remark=None):
    """新建任务（收旨时立即调用）"""
    _validate_new_task_id(task_id)
    # 清洗标题（剥离元数据）
    title = _sanitize_title(title)
    # 旨意标题校验
    valid, reason = _is_valid_task_title(title)
    if not valid:
        log.warning(f'⚠️ 拒绝创建 {task_id}：{reason}')
        print(f'[看板] 拒绝创建：{reason}', flush=True)
        return
    actual_org = STATE_ORG_MAP.get(state, org)
    clean_remark = _sanitize_remark(remark) if remark else f"下旨：{title}"
    outcome = {'created': False, 'duplicate': False, 'existing_state': ''}
    def modifier(tasks):
        existing = next((t for t in tasks if t.get('id') == task_id), None)
        if existing:
            outcome['duplicate'] = True
            outcome['existing_state'] = str(existing.get('state') or '')
            return tasks
        tasks = [t for t in tasks if t.get('id') != task_id]
        tasks.insert(0, {
            "id": task_id, "title": title, "official": official,
            "org": actual_org, "state": state,
            "now": clean_remark[:60] if remark else f"已下旨，等待{actual_org}接旨",
            "eta": "-", "block": "无", "output": "", "ac": "",
            "flow_log": [{"at": now_iso(), "from": "皇上", "to": actual_org, "remark": clean_remark}],
            "updatedAt": now_iso()
        })
        outcome['created'] = True
        return tasks
    atomic_json_update(TASKS_FILE, modifier, [])
    if outcome['duplicate']:
        raise KanbanInputError(
            f'任务 {task_id} 已存在 (state={outcome["existing_state"] or "unknown"})，禁止重复创建'
        )
    save(load())  # trigger refresh
    log.info(f'✅ 创建 {task_id} | {title[:30]} | state={state}')


def cmd_create_auto(title, state, org, official, remark=None):
    """原子分配当日下一个 JJC 编号并创建任务。成功时仅向 stdout 输出 task_id。"""
    title = _sanitize_title(title)
    valid, reason = _is_valid_task_title(title)
    if not valid:
        raise KanbanInputError(reason)
    actual_org = STATE_ORG_MAP.get(state, org)
    clean_remark = _sanitize_remark(remark) if remark else f"下旨：{title}"
    result = {'task_id': ''}

    def modifier(tasks):
        task_id = _next_jjc_task_id(tasks)
        result['task_id'] = task_id
        tasks.insert(0, {
            "id": task_id, "title": title, "official": official,
            "org": actual_org, "state": state,
            "now": clean_remark[:60] if remark else f"已下旨，等待{actual_org}接旨",
            "eta": "-", "block": "无", "output": "", "ac": "",
            "flow_log": [{"at": now_iso(), "from": "皇上", "to": actual_org, "remark": clean_remark}],
            "updatedAt": now_iso()
        })
        return tasks

    atomic_json_update(TASKS_FILE, modifier, [])
    save(load())  # trigger refresh
    task_id = result['task_id']
    if not task_id:
        raise KanbanInputError('自动创建任务失败：未生成任务ID')
    log.info(f'✅ 自动创建 {task_id} | {title[:30]} | state={state}')
    print(task_id)


def cmd_state(task_id, new_state, now_text=None):
    """更新任务状态（原子操作）"""
    old_state = [None]
    actor_agent = _infer_agent_id_from_runtime()
    def modifier(tasks):
        t = find_task(tasks, task_id)
        if not t:
            log.error(f'任务 {task_id} 不存在')
            return tasks
        _ensure_mutable_jjc_task(t, 'state', new_state)
        _validate_terminal_transition(t, actor_agent, new_state)
        if _is_jjc_task(t) and t.get('state') != new_state and t.get('state') not in ('Done', 'Cancelled'):
            _scheduler_snapshot(t, f'state-before-{new_state}')
        old_state[0] = t['state']
        t['state'] = new_state
        if new_state in STATE_ORG_MAP:
            t['org'] = STATE_ORG_MAP[new_state]
        if now_text:
            t['now'] = now_text
        _mark_scheduler_dispatch(t, 'kanban-state')
        _touch_scheduler_progress(t)
        t['updatedAt'] = now_iso()
        return tasks
    atomic_json_update(TASKS_FILE, modifier, [])
    save(load())  # trigger refresh
    log.info(f'✅ {task_id} 状态更新: {old_state[0]} → {new_state}')


def cmd_flow(task_id, from_dept, to_dept, remark):
    """添加流转记录（原子操作）"""
    clean_remark = _sanitize_remark(remark)
    def modifier(tasks):
        t = find_task(tasks, task_id)
        if not t:
            log.error(f'任务 {task_id} 不存在')
            return tasks
        _ensure_mutable_jjc_task(t, 'flow')
        if _is_jjc_task(t) and t.get('state') not in ('Done', 'Cancelled'):
            _scheduler_snapshot(t, f'flow-before-{from_dept}-to-{to_dept}')
        changed = _apply_flow_state_transition(t, from_dept, to_dept)
        t.setdefault('flow_log', []).append({
            "at": now_iso(), "from": from_dept, "to": to_dept, "remark": clean_remark
        })
        if changed:
            _mark_scheduler_dispatch(t, 'kanban-flow')
        _touch_scheduler_progress(t)
        t['updatedAt'] = now_iso()
        return tasks
    atomic_json_update(TASKS_FILE, modifier, [])
    save(load())  # trigger refresh
    log.info(f'✅ {task_id} 流转记录: {from_dept} → {to_dept}')


def cmd_done(task_id, output_path='', summary=''):
    """标记任务完成（原子操作）"""
    actor_agent = _infer_agent_id_from_runtime()
    def modifier(tasks):
        t = find_task(tasks, task_id)
        if not t:
            log.error(f'任务 {task_id} 不存在')
            return tasks
        _ensure_mutable_jjc_task(t, 'done')
        _validate_terminal_transition(t, actor_agent, 'Done')
        t['state'] = 'Done'
        t['org'] = STATE_ORG_MAP.get('Done', '完成')
        t['output'] = output_path
        t['now'] = summary or '任务已完成'
        _touch_scheduler_progress(t)
        t.setdefault('flow_log', []).append({
            "at": now_iso(), "from": t.get('org', '执行部门'),
            "to": "皇上", "remark": f"✅ 完成：{summary or '任务已完成'}"
        })
        t['updatedAt'] = now_iso()
        return tasks
    atomic_json_update(TASKS_FILE, modifier, [])
    save(load())  # trigger refresh
    if shiguan_hooks is not None:
        try:
            shiguan_hooks.trigger_archive(task_id, output_path, summary)
        except Exception:
            log.exception(f'⚠️ {task_id} 史官 done hook 触发失败')
    log.info(f'✅ {task_id} 已完成')


def cmd_block(task_id, reason):
    """标记阻塞（原子操作）"""
    def modifier(tasks):
        t = find_task(tasks, task_id)
        if not t:
            log.error(f'任务 {task_id} 不存在')
            return tasks
        _ensure_mutable_jjc_task(t, 'block')
        t['state'] = 'Blocked'
        t['org'] = STATE_ORG_MAP.get('Blocked', '阻塞')
        t['block'] = reason
        _touch_scheduler_progress(t)
        t['updatedAt'] = now_iso()
        return tasks
    atomic_json_update(TASKS_FILE, modifier, [])
    save(load())  # trigger refresh
    log.warning(f'⚠️ {task_id} 已阻塞: {reason}')


def cmd_progress(task_id, now_text, todos_pipe='', tokens=0, cost=0.0, elapsed=0):
    """🔥 实时进展汇报 — Agent 主动调用，不改变状态，只更新 now + todos

    now_text: 当前正在做什么的一句话描述（必填）
    todos_pipe: 可选，用 | 分隔的 todo 列表，格式：
        "已完成的事项✅|正在做的事项🔄|计划做的事项"
        - 以 ✅ 结尾 → completed
        - 以 🔄 结尾 → in-progress
        - 其他 → not-started
    tokens: 可选，本次消耗的 token 数
    cost: 可选，本次成本（美元）
    elapsed: 可选，本次耗时（秒）
    """
    clean = _sanitize_remark(now_text)
    # 解析 todos_pipe
    parsed_todos = None
    if todos_pipe:
        new_todos = []
        for i, item in enumerate(todos_pipe.split('|'), 1):
            item = item.strip()
            if not item:
                continue
            if item.endswith('✅'):
                status = 'completed'
                title = item[:-1].strip()
            elif item.endswith('🔄'):
                status = 'in-progress'
                title = item[:-1].strip()
            else:
                status = 'not-started'
                title = item
            new_todos.append({'id': str(i), 'title': title, 'status': status})
        if new_todos:
            parsed_todos = new_todos

    # 解析资源消耗参数
    try:
        tokens = int(tokens) if tokens else 0
    except (ValueError, TypeError):
        tokens = 0
    try:
        cost = float(cost) if cost else 0.0
    except (ValueError, TypeError):
        cost = 0.0
    try:
        elapsed = int(elapsed) if elapsed else 0
    except (ValueError, TypeError):
        elapsed = 0

    done_cnt = [0]
    total_cnt = [0]
    inferred_agent = ['']
    def modifier(tasks):
        t = find_task(tasks, task_id)
        if not t:
            log.error(f'任务 {task_id} 不存在')
            return tasks
        _ensure_mutable_jjc_task(t, 'progress')
        t['now'] = clean
        if parsed_todos is not None:
            t['todos'] = parsed_todos
        # 多 Agent 并行进展日志
        at = now_iso()
        agent_id = _infer_agent_id_from_runtime(t)
        inferred_agent[0] = agent_id
        agent_label = _AGENT_LABELS.get(agent_id, agent_id)
        log_todos = parsed_todos if parsed_todos is not None else t.get('todos', [])
        log_entry = {
            'at': at, 'agent': agent_id, 'agentLabel': agent_label,
            'text': clean, 'todos': log_todos,
            'state': t.get('state', ''), 'org': t.get('org', ''),
        }
        # 资源消耗（可选字段，有值才写入）
        if tokens > 0:
            log_entry['tokens'] = tokens
        if cost > 0:
            log_entry['cost'] = cost
        if elapsed > 0:
            log_entry['elapsed'] = elapsed
        t.setdefault('progress_log', []).append(log_entry)
        # 限制 progress_log 大小，防止无限增长
        if len(t['progress_log']) > MAX_PROGRESS_LOG:
            t['progress_log'] = t['progress_log'][-MAX_PROGRESS_LOG:]
        _touch_scheduler_progress(t)
        t['updatedAt'] = at
        done_cnt[0] = sum(1 for td in t.get('todos', []) if td.get('status') == 'completed')
        total_cnt[0] = len(t.get('todos', []))
        return tasks
    atomic_json_update(TASKS_FILE, modifier, [])
    save(load())  # trigger refresh
    if shiguan_hooks is not None and inferred_agent[0]:
        try:
            shiguan_hooks.buffer_progress(task_id, clean, todos_pipe, inferred_agent[0])
        except Exception:
            log.exception(f'⚠️ {task_id} 史官 progress hook 写入失败')
    res_info = ''
    if tokens or cost or elapsed:
        res_info = f' [res: {tokens}tok/${cost:.4f}/{elapsed}s]'
    log.info(f'📡 {task_id} 进展: {clean[:40]}... [{done_cnt[0]}/{total_cnt[0]}]{res_info}')

def cmd_todo(task_id, todo_id, title, status='not-started', detail=''):
    """添加或更新子任务 todo（原子操作）

    status: not-started / in-progress / completed
    detail: 可选，该子任务的详细产出/说明（Markdown 格式）
    """
    # 校验 status 值
    if status not in ('not-started', 'in-progress', 'completed'):
        status = 'not-started'
    result_info = [0, 0]
    def modifier(tasks):
        t = find_task(tasks, task_id)
        if not t:
            log.error(f'任务 {task_id} 不存在')
            return tasks
        _ensure_mutable_jjc_task(t, 'todo')
        if 'todos' not in t:
            t['todos'] = []
        existing = next((td for td in t['todos'] if str(td.get('id')) == str(todo_id)), None)
        if existing:
            existing['status'] = status
            if title:
                existing['title'] = title
            if detail:
                existing['detail'] = detail
        else:
            item = {'id': todo_id, 'title': title, 'status': status}
            if detail:
                item['detail'] = detail
            t['todos'].append(item)
        _touch_scheduler_progress(t)
        t['updatedAt'] = now_iso()
        result_info[0] = sum(1 for td in t['todos'] if td.get('status') == 'completed')
        result_info[1] = len(t['todos'])
        return tasks
    atomic_json_update(TASKS_FILE, modifier, [])
    save(load())  # trigger refresh
    log.info(f'✅ {task_id} todo [{result_info[0]}/{result_info[1]}]: {todo_id} → {status}')

_CMD_MIN_ARGS = {
    'create': 6, 'create_auto': 5, 'state': 3, 'flow': 5, 'done': 2, 'block': 3, 'todo': 4, 'progress': 3,
}

if __name__ == '__main__':
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)
    cmd = args[0]
    if cmd in _CMD_MIN_ARGS and len(args) < _CMD_MIN_ARGS[cmd]:
        print(f'错误："{cmd}" 命令至少需要 {_CMD_MIN_ARGS[cmd]} 个参数，实际 {len(args)} 个')
        print(__doc__)
        sys.exit(1)
    try:
        if cmd == 'create':
            cmd_create(args[1], args[2], args[3], args[4], args[5], args[6] if len(args)>6 else None)
        elif cmd == 'create_auto':
            cmd_create_auto(args[1], args[2], args[3], args[4], args[5] if len(args)>5 else None)
        elif cmd == 'state':
            cmd_state(args[1], args[2], args[3] if len(args)>3 else None)
        elif cmd == 'flow':
            cmd_flow(args[1], args[2], args[3], args[4])
        elif cmd == 'done':
            cmd_done(args[1], args[2] if len(args)>2 else '', args[3] if len(args)>3 else '')
        elif cmd == 'block':
            cmd_block(args[1], args[2])
        elif cmd == 'todo':
            # 解析可选 --detail 参数
            todo_pos = []
            todo_detail = ''
            ti = 1
            while ti < len(args):
                if args[ti] == '--detail' and ti + 1 < len(args):
                    todo_detail = args[ti + 1]; ti += 2
                else:
                    todo_pos.append(args[ti]); ti += 1
            cmd_todo(
                todo_pos[0] if len(todo_pos) > 0 else '',
                todo_pos[1] if len(todo_pos) > 1 else '',
                todo_pos[2] if len(todo_pos) > 2 else '',
                todo_pos[3] if len(todo_pos) > 3 else 'not-started',
                detail=todo_detail,
            )
        elif cmd == 'progress':
            # 解析可选 --tokens/--cost/--elapsed 参数
            pos_args = []
            kw = {}
            i = 1
            while i < len(args):
                if args[i] == '--tokens' and i + 1 < len(args):
                    kw['tokens'] = args[i + 1]; i += 2
                elif args[i] == '--cost' and i + 1 < len(args):
                    kw['cost'] = args[i + 1]; i += 2
                elif args[i] == '--elapsed' and i + 1 < len(args):
                    kw['elapsed'] = args[i + 1]; i += 2
                else:
                    pos_args.append(args[i]); i += 1
            cmd_progress(
                pos_args[0] if len(pos_args) > 0 else '',
                pos_args[1] if len(pos_args) > 1 else '',
                pos_args[2] if len(pos_args) > 2 else '',
                tokens=kw.get('tokens', 0),
                cost=kw.get('cost', 0.0),
                elapsed=kw.get('elapsed', 0),
            )
        else:
            print(__doc__)
            sys.exit(1)
    except KanbanInputError as exc:
        log.warning(f'⚠️ 参数错误: {exc}')
        print(f'[看板] 参数错误：{exc}')
        sys.exit(2)
