"""史官（shiguan）钩子 — 供 kanban_update.py 调用"""
import json
import os
import pathlib
import subprocess

_SHIGUAN_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent


def _now_iso():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def buffer_progress(task_id, text, todos_pipe, agent_id):
    """史官缓冲：将 progress 追加到磁盘（零 API 开销）"""
    try:
        buf_dir = _SHIGUAN_ROOT / 'workspace-shiguan' / 'data' / 'buffer'
        buf_dir.mkdir(parents=True, exist_ok=True)
        with open(buf_dir / f'{task_id}.jsonl', 'a', encoding='utf-8') as fh:
            fh.write(json.dumps({
                'at': _now_iso(), 'agent': agent_id,
                'text': text, 'todos': todos_pipe
            }, ensure_ascii=False) + '\n')
    except Exception:
        pass


def trigger_archive(task_id, output_path, summary):
    """史官归档：异步触发 Kimi K2.5 提取（不阻塞调用方）"""
    try:
        script = _SHIGUAN_ROOT / 'workspace-shiguan' / 'scripts' / 'archive_daemon.py'
        if script.exists():
            subprocess.Popen(
                ['python3', str(script), '--task-id', task_id,
                 '--output', output_path or '', '--summary', summary or ''],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    except Exception:
        pass
