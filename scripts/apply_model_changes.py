#!/usr/bin/env python3
"""应用 data/pending_model_changes.json → openclaw.json，并重启 Gateway"""
import json, pathlib, subprocess, datetime, shutil, logging, glob, os, time, fcntl
from contextlib import contextmanager
from file_lock import atomic_json_write, atomic_json_read, atomic_json_update

log = logging.getLogger('model_change')
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(message)s', datefmt='%H:%M:%S')

BASE = pathlib.Path(__file__).parent.parent
DATA = BASE / 'data'
OPENCLAW_CFG = pathlib.Path.home() / '.openclaw' / 'openclaw.json'
SUBAGENT_RUNS = pathlib.Path.home() / '.openclaw' / 'subagents' / 'runs.json'
PENDING = DATA / 'pending_model_changes.json'
CHANGE_LOG = DATA / 'model_change_log.json'
MAX_BACKUPS = 10
ACTIVE_RUN_GUARD_SEC = int(os.environ.get('MODEL_CHANGE_ACTIVE_RUN_GUARD_SEC', '1800'))
GLOBAL_APPLY_LOCK = OPENCLAW_CFG.parent / 'openclaw.model_changes.lock'


def _lock_path(path: pathlib.Path) -> pathlib.Path:
    return path.parent / (path.name + '.lock')


@contextmanager
def advisory_lock(path: pathlib.Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a+', encoding='utf-8') as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def rj(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def cleanup_backups():
    """只保留最近 MAX_BACKUPS 个备份"""
    pattern = str(OPENCLAW_CFG.parent / 'openclaw.json.bak.model-*')
    baks = sorted(glob.glob(pattern))
    for old in baks[:-MAX_BACKUPS]:
        try:
            pathlib.Path(old).unlink()
        except OSError:
            pass


def has_recent_active_subagent_run():
    """
    检查是否存在“近期未结束”的子任务运行。
    若存在，暂缓模型变更，避免 Gateway 重启打断在途任务。
    """
    data = rj(SUBAGENT_RUNS, {})
    runs = data.get('runs', {})
    if isinstance(runs, dict):
        items = runs.values()
    elif isinstance(runs, list):
        items = runs
    else:
        return False

    now_ms = int(time.time() * 1000)
    guard_ms = max(ACTIVE_RUN_GUARD_SEC, 60) * 1000

    for run in items:
        if not isinstance(run, dict):
            continue
        if run.get('endedAt'):
            continue
        started = run.get('startedAt') or run.get('createdAt') or 0
        if isinstance(started, (int, float)) and started > 0 and (now_ms - int(started) <= guard_ms):
            return True
    return False


def main():
    if not PENDING.exists():
        return
    with advisory_lock(GLOBAL_APPLY_LOCK):
        with advisory_lock(_lock_path(PENDING)):
            pending = rj(PENDING, [])
            if not pending:
                return
            if has_recent_active_subagent_run():
                log.info(f'active subagent run detected; defer model changes ({len(pending)})')
                return

            now_str = datetime.datetime.now().isoformat()
            ready_to_apply = []
            deferred = []
            for item in pending:
                revert_at = item.get('autoRevertAt', '')
                if revert_at and revert_at > now_str:
                    deferred.append(item)
                else:
                    ready_to_apply.append(item)

            if not ready_to_apply and not deferred:
                return
            if not ready_to_apply:
                return

            applied, errors = [], []
            backup_stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
            bak = OPENCLAW_CFG.parent / f'openclaw.json.bak.model-{backup_stamp}'
            shutil.copy2(OPENCLAW_CFG, bak)
            cleanup_backups()

            def apply_changes(cfg):
                cfg = cfg if isinstance(cfg, dict) else {}
                agents = cfg.setdefault('agents', {})
                defaults = agents.setdefault('defaults', {})
                default_model = defaults.get('model', {}).get('primary', '') if isinstance(defaults.get('model', {}), dict) else ''
                agents_list = agents.get('list', [])
                if not isinstance(agents_list, list):
                    agents_list = []
                    agents['list'] = agents_list

                for change in ready_to_apply:
                    ag_id = change.get('agentId', '').strip()
                    new_model = change.get('model', '').strip()
                    if not ag_id or not new_model:
                        errors.append({'change': change, 'error': 'missing fields'})
                        continue
                    found = False
                    for ag in agents_list:
                        if ag.get('id') == ag_id:
                            old = ag.get('model', default_model)
                            if new_model == default_model:
                                ag.pop('model', None)
                            else:
                                ag['model'] = new_model
                            applied.append({
                                'at': datetime.datetime.now().isoformat(),
                                'agentId': ag_id,
                                'oldModel': old,
                                'newModel': new_model,
                            })
                            found = True
                            break
                    if not found:
                        errors.append({'change': change, 'error': f'agent {ag_id} not found'})
                agents['list'] = agents_list
                return cfg

            atomic_json_update(OPENCLAW_CFG, apply_changes, {})

            if applied:
                log_data = rj(CHANGE_LOG, [])
                if not isinstance(log_data, list):
                    log.warning('model_change_log.json is not a list; reset to []')
                    log_data = []
                log_data.extend(applied)
                if len(log_data) > 200:
                    log_data = log_data[-200:]
                atomic_json_write(CHANGE_LOG, log_data)

                for e in applied:
                    log.info(f'{e["agentId"]}: {e["oldModel"]} → {e["newModel"]}')

                restart_ok = False
                rollback = False
                try:
                    r = subprocess.run(['openclaw', 'gateway', 'restart'], capture_output=True, text=True, timeout=30)
                    restart_ok = r.returncode == 0
                    log.info(f'gateway restart rc={r.returncode}')
                except Exception as e:
                    log.error(f'gateway restart failed: {e}')
                    if bak.exists():
                        shutil.copy2(bak, OPENCLAW_CFG)
                        log.warning('rolled back openclaw.json from backup')
                        rollback = True
                        for a in applied:
                            a['rolledBack'] = True

                atomic_json_write(PENDING, deferred)
                atomic_json_write(DATA / 'last_model_change_result.json', {
                    'at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'applied': applied, 'errors': errors,
                    'gatewayRestarted': restart_ok, 'rolledBack': rollback,
                    'deferred': len(deferred),
                })
            elif errors:
                log.warning(f'{len(errors)} changes failed, 0 applied')
                atomic_json_write(PENDING, deferred)


if __name__ == '__main__':
    main()
