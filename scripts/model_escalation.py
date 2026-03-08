#!/usr/bin/env python3
"""
动态模型升降级引擎 — 三省六部调度系统

升级策略：
  - 关键词触发（4大领域17条规则）
  - 中书省指令 [LEVEL: HIGH_SPEC]
  - 报错/封驳/超时重试升级
  - 史官循环检测信号
  - 影子审核 FAIL 触发

降级策略：
  - 绑定任务生命周期，状态转移离开 agent 时自动降级
  - scheduler scan 兜底清理孤立升级
"""
import json
import pathlib
import re
import datetime
import subprocess
import sys
import logging
import os
import urllib.request

log = logging.getLogger('model_escalation')

BASE = pathlib.Path(__file__).resolve().parent.parent
DATA = BASE / 'data'
SCRIPTS = pathlib.Path(__file__).resolve().parent
OPENCLAW_CFG = pathlib.Path.home() / '.openclaw' / 'openclaw.json'
DYNAMIC_UPGRADE_CFG = pathlib.Path.home() / '.openclaw' / 'dynamic_upgrade.backup.json'
UPGRADE_STATE = DATA / 'model_upgrade_state.json'
UPGRADE_LOG = DATA / 'model_upgrade_log.json'
CHECKPOINT_DIR = pathlib.Path.home() / '.openclaw' / 'workspace-shiguan' / 'data' / 'checkpoints'
BUFFER_DIR = pathlib.Path.home() / '.openclaw' / 'workspace-shiguan' / 'data' / 'buffer'

sys.path.insert(0, str(SCRIPTS))
from file_lock import atomic_json_read, atomic_json_write, atomic_json_update


# ══ 配置加载 ══

def _load_dynamic_upgrade_from_separate_file():
    """从独立配置文件读取 dynamic_upgrade 配置。"""
    return atomic_json_read(DYNAMIC_UPGRADE_CFG, {})


def load_upgrade_config():
    """优先读取 openclaw.json 的 dynamic_upgrade，缺失时回退到独立配置文件。"""
    cfg = atomic_json_read(OPENCLAW_CFG, {})
    dup = cfg.get('dynamic_upgrade')
    if not isinstance(dup, dict) or not dup:
        dup = _load_dynamic_upgrade_from_separate_file()
    if not isinstance(dup, dict) or not dup.get('enabled', False):
        return None
    return dup


def _load_env():
    """加载 .env 文件中的 API key（用于 checkpoint 生成和影子审核）。"""
    env_path = pathlib.Path.home() / '.openclaw' / '.env'
    result = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                result[k.strip()] = v.strip()
    return result


# ══ Tier 解析 ══

def get_model_for_tier(tier, tiers_config):
    """根据层级返回第一个模型字符串。超出范围则 clamp 到最大层级。"""
    tier = int(tier)
    for t in tiers_config:
        if t['level'] == tier:
            return t['models'][0]
    # Clamp to max
    if tiers_config:
        max_tier = max(t['level'] for t in tiers_config)
        for t in tiers_config:
            if t['level'] == max_tier:
                return t['models'][0]
    return None


def get_tier_for_model(model_str, tiers_config):
    """根据模型字符串反查所在层级。"""
    for t in tiers_config:
        if model_str in t.get('models', []):
            return t['level']
    return 0


# ══ 触发检测 ══

def check_keyword_triggers(text, keyword_config):
    """扫描文本，返回 (max_bump, matched_categories)。"""
    if not keyword_config.get('enabled', False) or not text:
        return 0, []
    max_bump = 0
    categories = []
    for pattern_cfg in keyword_config.get('patterns', []):
        regex = pattern_cfg.get('regex', '')
        bump = int(pattern_cfg.get('bump', 1))
        category = pattern_cfg.get('category', '')
        try:
            if re.search(regex, text, re.IGNORECASE):
                if bump > max_bump:
                    max_bump = bump
                if category and category not in categories:
                    categories.append(category)
        except re.error:
            log.warning(f'Invalid regex in keyword_triggers: {regex}')
    return max_bump, categories


def check_directive_trigger(text, directive_config):
    """检测 [LEVEL: HIGH_SPEC] 标签。"""
    if not directive_config.get('enabled', False) or not text:
        return 0
    tag_pattern = directive_config.get('tag_pattern', r'\[LEVEL:\s*HIGH_SPEC\]')
    try:
        if re.search(tag_pattern, text, re.IGNORECASE):
            return int(directive_config.get('bump', 2))
    except re.error:
        pass
    return 0


# ══ Checkpoint 生成（史官 K2.5 压缩摘要）══

def generate_checkpoint(task, agent_id, reason):
    """
    由史官 K2.5 生成 ≤800 token 压缩摘要。
    通过 NewAPI HTTP 直调，不走 agent 派发，不改模型，不重启 Gateway。
    返回摘要文本。失败时降级为本地数据组装。
    """
    task_id = task.get('id', '?')

    # 收集原始数据
    raw_context = _collect_raw_context(task, agent_id)

    # 尝试调用 K2.5 生成摘要
    cfg = load_upgrade_config()
    checkpoint_cfg = (cfg or {}).get('checkpoint', {})
    if checkpoint_cfg.get('enabled', True):
        try:
            summary = _call_k25_for_checkpoint(raw_context, task_id, agent_id, reason)
            if summary:
                # 存入文件
                CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
                cp_file = CHECKPOINT_DIR / f'{task_id}_{agent_id}.md'
                cp_file.write_text(summary, encoding='utf-8')
                log.info(f'[Checkpoint] {task_id}_{agent_id}: {len(summary)} chars')
                return summary
        except Exception as e:
            log.warning(f'[Checkpoint] K2.5 调用失败，降级为本地摘要: {e}')

    # 降级：本地数据组装
    return _fallback_checkpoint(task, agent_id, reason)


def _collect_raw_context(task, agent_id):
    """从 buffer 和 task 数据收集原始上下文。"""
    parts = []

    # 从 shiguan buffer 读取该 agent 的进展
    task_id = task.get('id', '?')
    buf_file = BUFFER_DIR / f'{task_id}.jsonl'
    if buf_file.exists():
        entries = []
        try:
            for line in buf_file.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get('agent') == agent_id:
                        entries.append(entry)
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass
        # 取最近 10 条
        for e in entries[-10:]:
            parts.append(f"[{e.get('at', '?')[:19]}] {e.get('text', '')[:200]}")

    # flow_log 最近 5 条
    for fl in task.get('flow_log', [])[-5:]:
        parts.append(f"流转: {fl.get('from', '?')} → {fl.get('to', '?')}: {fl.get('remark', '')[:100]}")

    # progress_log 最近 3 条
    for pl in task.get('progress_log', [])[-3:]:
        parts.append(f"进展: [{pl.get('agent', '?')}] {pl.get('text', '')[:150]}")

    return '\n'.join(parts) if parts else '(无历史记录)'


def _call_k25_for_checkpoint(raw_context, task_id, agent_id, reason):
    """通过 NewAPI HTTP 直调 K2.5 生成压缩摘要。"""
    env = _load_env()
    base_url = env.get('NEWAPI_BASE_URL', 'http://127.0.0.1:3001')
    api_key = env.get('NEWAPI_API_KEY', '')
    if not api_key:
        return None

    prompt = (
        f"你是三省六部系统的史官。请为即将接手的高阶模型生成一份简洁的上下文摘要。\n"
        f"要求：不超过 800 字，包含以下要点：\n"
        f"1. 当前任务进展到哪一步了\n"
        f"2. 之前尝试了什么，失败了什么\n"
        f"3. 代码/方案当前状态\n"
        f"4. 关键决策和约束\n\n"
        f"升级原因: {reason}\n"
        f"任务ID: {task_id}, Agent: {agent_id}\n\n"
        f"原始记录:\n{raw_context[:3000]}"
    )

    payload = json.dumps({
        "model": "kimi-k2.5",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1000,
        "temperature": 0.3,
    }).encode('utf-8')

    req = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
        data=payload,
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}',
        },
        method='POST',
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode('utf-8'))
            choices = result.get('choices', [])
            if choices:
                content = choices[0].get('message', {}).get('content', '')
                return f"═══ 【模型升级·史官摘要】═══\n{content}\n═══════════════════════════\n"
    except Exception as e:
        log.warning(f'K2.5 checkpoint HTTP call failed: {e}')
    return None


def _fallback_checkpoint(task, agent_id, reason):
    """本地数据组装的降级摘要（零 API 开销）。"""
    task_id = task.get('id', '?')
    state = task.get('state', '')
    org = task.get('org', '')
    now_text = task.get('now', '')

    flow_lines = []
    for fl in task.get('flow_log', [])[-3:]:
        flow_lines.append(f"  {fl.get('from', '?')} → {fl.get('to', '?')}: {fl.get('remark', '')[:80]}")

    progress_lines = []
    for pl in task.get('progress_log', [])[-2:]:
        progress_lines.append(f"  [{pl.get('agent', '?')}] {pl.get('text', '')[:100]}")

    return (
        f"═══ 【模型升级·本地摘要】═══\n"
        f"触发原因: {reason}\n"
        f"任务: {task_id} | 状态: {state} | 部门: {org}\n"
        f"当前: {now_text}\n"
        f"最近流转:\n{''.join(flow_lines) or '  (无)'}\n"
        f"最近进展:\n{''.join(progress_lines) or '  (无)'}\n"
        f"═══════════════════════════\n"
    )


# ══ 升级决策 ══

def decide_upgrade(agent_id, task, dispatch_msg, trigger_type, cfg):
    """
    评估是否需要升级 agent 的模型。
    返回 dict: {should_upgrade, new_tier, new_model, reason, bump}
    """
    result = {
        'should_upgrade': False, 'new_tier': 0, 'new_model': '',
        'reason': '', 'bump': 0,
    }

    if not cfg or not cfg.get('enabled', False):
        return result

    # 速率限制
    state = atomic_json_read(UPGRADE_STATE, None) or {}
    hourly_count = int(state.get('hourly_upgrade_count', 0))
    max_per_hour = int(cfg.get('max_upgrades_per_hour', 5))

    # 重置小时计数器
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    last_reset = state.get('last_hour_reset', '')
    try:
        last_reset_dt = datetime.datetime.fromisoformat(last_reset)
        if (now_dt - last_reset_dt).total_seconds() > 3600:
            hourly_count = 0
    except Exception:
        pass

    if hourly_count >= max_per_hour:
        log.warning(f'[升级限制] 每小时上限 {max_per_hour} 已达，跳过')
        return result

    tiers_config = cfg.get('tiers', [])
    agent_base_tiers = cfg.get('agent_base_tiers', {})
    base_tier = int(agent_base_tiers.get(agent_id, 0))
    max_tier = max((t['level'] for t in tiers_config), default=3)

    # 当前层级（可能已被升级）
    agent_state = state.get('agents', {}).get(agent_id, {})
    current_tier = int(agent_state.get('currentTier', base_tier))

    bump = 0
    reason_parts = []

    # 1. 关键词触发
    kw_cfg = cfg.get('keyword_triggers', {})
    check_text = f"{task.get('title', '')} {task.get('now', '')} {dispatch_msg}"
    kw_bump, kw_cats = check_keyword_triggers(check_text, kw_cfg)
    if kw_bump:
        bump = max(bump, kw_bump)
        cats_str = '+'.join(kw_cats) if kw_cats else ''
        reason_parts.append(f'keyword[{cats_str}](bump={kw_bump})')

    # 2. 指令标签
    dir_cfg = cfg.get('directive_triggers', {})
    dir_text = f"{task.get('title', '')} {task.get('now', '')} {task.get('output', '')}"
    dir_bump = check_directive_trigger(dir_text, dir_cfg)
    if dir_bump:
        bump = max(bump, dir_bump)
        reason_parts.append(f'directive[HIGH_SPEC](bump={dir_bump})')

    # 3. 失败升级
    sched = task.get('_scheduler', {})
    fail_cfg = cfg.get('failure_escalation', {})
    if fail_cfg.get('enabled', True):
        last_status = sched.get('lastDispatchStatus', '')

        # 三次失败检测
        retry_count = int(sched.get('retryCount', 0))
        if retry_count >= 3:
            thrice_bump = int(fail_cfg.get('bump_on_thrice_failure', 2))
            bump = max(bump, thrice_bump)
            reason_parts.append(f'thrice-failure(bump={thrice_bump})')
        elif trigger_type in ('taizi-scan-retry', 'taizi-retry'):
            fail_bump = int(fail_cfg.get('bump_on_error', 1))
            bump = max(bump, fail_bump)
            reason_parts.append(f'retry-escalation(bump={fail_bump})')
        elif last_status == 'rejected':
            rej_bump = int(fail_cfg.get('bump_on_rejection', 1))
            bump = max(bump, rej_bump)
            reason_parts.append(f'rejection(bump={rej_bump})')
        elif last_status == 'timeout':
            to_bump = int(fail_cfg.get('bump_on_timeout', 1))
            bump = max(bump, to_bump)
            reason_parts.append(f'timeout(bump={to_bump})')
        elif last_status in ('failed', 'error'):
            err_bump = int(fail_cfg.get('bump_on_error', 1))
            bump = max(bump, err_bump)
            reason_parts.append(f'dispatch-{last_status}(bump={err_bump})')

        # Clamp failure bump
        max_fail = int(fail_cfg.get('max_failure_bump', 2))
        if bump > max_fail and not kw_bump and not dir_bump:
            bump = max_fail

    # 4. 史官循环检测信号
    loop_bump = int(sched.get('pendingLoopUpgradeBump', 0))
    if loop_bump > 0:
        bump = max(bump, loop_bump)
        reason_parts.append(f'historian-loop(bump={loop_bump})')

    if bump == 0:
        return result

    new_tier = min(current_tier + bump, max_tier)
    if new_tier <= current_tier:
        return result  # 已在目标层级或更高

    new_model = get_model_for_tier(new_tier, tiers_config)
    if not new_model:
        return result

    reason = '+'.join(reason_parts)
    result.update({
        'should_upgrade': True,
        'new_tier': new_tier,
        'new_model': new_model,
        'reason': reason,
        'bump': bump,
    })
    return result


# ══ 升级执行 ══

def apply_upgrade(agent_id, new_tier, new_model, task_id, reason):
    """
    执行模型升级：写入 pending → 调用 apply_model_changes.py → 记录状态。
    返回 (ok, message)。
    """
    pending_path = DATA / 'pending_model_changes.json'

    # 写入 pending
    def update_pending(current):
        current = [x for x in (current or []) if x.get('agentId') != agent_id]
        current.append({'agentId': agent_id, 'model': new_model})
        return current
    atomic_json_update(pending_path, update_pending, [])

    # 同步执行 apply_model_changes.py
    try:
        r = subprocess.run(
            ['python3', str(SCRIPTS / 'apply_model_changes.py')],
            capture_output=True, text=True, timeout=35,
        )
        if r.returncode != 0:
            log.error(f'apply_model_changes failed for {agent_id}: {r.stderr[:200]}')
            return False, f'Gateway restart failed: {r.stderr[:100]}'
    except subprocess.TimeoutExpired:
        return False, 'apply_model_changes timed out'
    except Exception as e:
        return False, str(e)

    # 记录升级状态
    now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
    cfg = load_upgrade_config() or {}
    agent_base_tiers = cfg.get('agent_base_tiers', {})
    base_tier = int(agent_base_tiers.get(agent_id, 0))

    def update_state(s):
        if s is None:
            s = {'agents': {}, 'hourly_upgrade_count': 0, 'last_hour_reset': now_str}
        agents = s.setdefault('agents', {})
        agents[agent_id] = {
            'currentTier': new_tier,
            'baseTier': base_tier,
            'upgradeReason': reason,
            'taskId': task_id,
            'upgradedAt': now_str,
        }
        # 小时计数
        try:
            last_reset = s.get('last_hour_reset', now_str)
            last_dt = datetime.datetime.fromisoformat(last_reset)
            now_dt = datetime.datetime.now(datetime.timezone.utc)
            if (now_dt - last_dt).total_seconds() > 3600:
                s['hourly_upgrade_count'] = 0
                s['last_hour_reset'] = now_str
        except Exception:
            pass
        s['hourly_upgrade_count'] = s.get('hourly_upgrade_count', 0) + 1
        return s
    atomic_json_update(UPGRADE_STATE, update_state, None)

    # 追加日志
    _append_upgrade_log({
        'at': now_str, 'agentId': agent_id, 'newTier': new_tier,
        'newModel': new_model, 'taskId': task_id, 'reason': reason,
        'type': 'upgrade',
    })

    log.info(f'[升级] {agent_id} → tier {new_tier} ({new_model}) 原因: {reason} 任务: {task_id}')
    return True, f'{agent_id} upgraded to tier {new_tier}'


# ══ 降级（任务状态转移时触发）══

def revert_agent_on_transition(agent_id, task_id):
    """
    任务状态转移离开 agent 时调用。
    如果该 agent 因此 task 被升级过，降回 base tier。
    返回 (reverted, message)。
    """
    state = atomic_json_read(UPGRADE_STATE, None)
    if not state:
        return False, 'no state'
    agent_state = state.get('agents', {}).get(agent_id)
    if not agent_state:
        return False, 'not upgraded'

    # 只降级因该任务触发的升级
    if agent_state.get('taskId') != task_id:
        return False, f'upgrade was for different task: {agent_state.get("taskId")}'

    return _do_revert(agent_id, agent_state)


def cleanup_orphan_upgrades(active_task_ids):
    """
    Scheduler scan 调用：清理 task 已完成但 agent 未降级的孤立升级。
    active_task_ids: 所有非终态的 task id 集合。
    """
    state = atomic_json_read(UPGRADE_STATE, None)
    if not state or not state.get('agents'):
        return []

    reverted = []
    for ag_id, ag_state in list(state.get('agents', {}).items()):
        bound_task = ag_state.get('taskId', '')
        if bound_task and bound_task not in active_task_ids:
            ok, msg = _do_revert(ag_id, ag_state)
            if ok:
                reverted.append(ag_id)
                log.info(f'[孤立降级] {ag_id}: task {bound_task} 已结束')
    return reverted


def _do_revert(agent_id, agent_state):
    """执行降级操作。"""
    cfg = load_upgrade_config() or {}
    tiers_config = cfg.get('tiers', [])
    base_tier = int(agent_state.get('baseTier', 0))
    base_model = get_model_for_tier(base_tier, tiers_config)
    if not base_model:
        return False, 'cannot determine base model'

    pending_path = DATA / 'pending_model_changes.json'

    def update_pending(current):
        current = [x for x in (current or []) if x.get('agentId') != agent_id]
        current.append({'agentId': agent_id, 'model': base_model})
        return current
    atomic_json_update(pending_path, update_pending, [])

    try:
        r = subprocess.run(
            ['python3', str(SCRIPTS / 'apply_model_changes.py')],
            capture_output=True, text=True, timeout=35,
        )
        if r.returncode != 0:
            return False, f'revert gateway restart failed: {r.stderr[:100]}'
    except Exception as e:
        return False, str(e)

    # 清除升级状态
    def clear_state(s):
        if s and 'agents' in s:
            s['agents'].pop(agent_id, None)
        return s
    atomic_json_update(UPGRADE_STATE, clear_state, None)

    now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _append_upgrade_log({
        'at': now_str, 'agentId': agent_id, 'newTier': base_tier,
        'newModel': base_model, 'taskId': agent_state.get('taskId', ''),
        'reason': 'state_transition_revert', 'type': 'revert',
    })

    log.info(f'[降级] {agent_id} → tier {base_tier} ({base_model})')
    return True, f'{agent_id} reverted to tier {base_tier}'


# ══ 影子审核 ══

def shadow_review(code_text, task_id, agent_id, cfg):
    """
    影子审核模式：Opus 4.6 只做 PASS/FAIL 审核。
    直接 HTTP 调用，不改 agent 模型，不重启 Gateway。
    返回 dict: {passed, reason, model}
    """
    shadow_cfg = cfg.get('shadow_review', {})
    if not shadow_cfg.get('enabled', False):
        return {'passed': True, 'reason': 'shadow review disabled', 'model': ''}

    env = _load_env()
    base_url = env.get('NEWAPI_BASE_URL', 'http://127.0.0.1:3001')
    api_key = env.get('NEWAPI_API_KEY', '')
    if not api_key:
        return {'passed': True, 'reason': 'no API key', 'model': ''}

    reviewer_model = shadow_cfg.get('reviewer_model', 'claude-opus-4-6')
    # 从 provider/model 格式中提取模型 ID
    model_id = reviewer_model.split('/')[-1] if '/' in reviewer_model else reviewer_model

    prompt_template = shadow_cfg.get(
        'prompt_template',
        "你是高级代码审查员。只回答 PASS 或 FAIL + 原因（50字以内）。审查以下代码的安全性和正确性：\n\n{code}"
    )
    prompt = prompt_template.replace('{code}', code_text[:5000])

    payload = json.dumps({
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 100,
        "temperature": 0,
    }).encode('utf-8')

    req = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
        data=payload,
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}',
        },
        method='POST',
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode('utf-8'))
            choices = result.get('choices', [])
            if choices:
                content = choices[0].get('message', {}).get('content', '').strip()
                passed = content.upper().startswith('PASS')
                return {'passed': passed, 'reason': content[:200], 'model': model_id}
    except Exception as e:
        log.warning(f'Shadow review HTTP call failed: {e}')

    return {'passed': True, 'reason': 'review call failed, defaulting to pass', 'model': model_id}


# ══ 内部工具 ══

def _append_upgrade_log(entry):
    """追加到升级日志文件（保留最近 500 条）。"""
    def updater(current):
        current = current or []
        current.append(entry)
        if len(current) > 500:
            current = current[-500:]
        return current
    atomic_json_update(UPGRADE_LOG, updater, [])


def get_upgrade_state():
    """读取当前升级状态（供 API 端点使用）。"""
    return atomic_json_read(UPGRADE_STATE, {})


def get_upgrade_log(limit=100):
    """读取升级日志（供 API 端点使用）。"""
    data = atomic_json_read(UPGRADE_LOG, [])
    return data[-limit:] if data else []
