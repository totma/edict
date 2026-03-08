"""tests for scripts/kanban_update.py"""
import json, pathlib, sys
import pytest

# Ensure scripts/ is importable
SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / 'scripts'
sys.path.insert(0, str(SCRIPTS))

import kanban_update as kb


def test_create_and_get(tmp_path):
    """kanban create + get round-trip."""
    tasks_file = tmp_path / 'tasks_source.json'
    tasks_file.write_text('[]')

    # Patch TASKS_FILE
    original = kb.TASKS_FILE
    kb.TASKS_FILE = tasks_file
    try:
        kb.cmd_create('TEST-001', '测试任务创建和查询功能验证', 'Inbox', '工部', '工部尚书')
        tasks = json.loads(tasks_file.read_text())
        assert any(t.get('id') == 'TEST-001' for t in tasks)
        t = next(t for t in tasks if t['id'] == 'TEST-001')
        assert t['title'] == '测试任务创建和查询功能验证'
        assert t['state'] == 'Inbox'
        assert t['org'] == '工部'
    finally:
        kb.TASKS_FILE = original


def test_move_state(tmp_path):
    """kanban move changes task state."""
    tasks_file = tmp_path / 'tasks_source.json'
    tasks_file.write_text(json.dumps([
        {'id': 'T-1', 'title': 'test', 'state': 'Inbox'}
    ]))

    original = kb.TASKS_FILE
    kb.TASKS_FILE = tasks_file
    try:
        kb.cmd_state('T-1', 'Doing')
        tasks = json.loads(tasks_file.read_text())
        assert tasks[0]['state'] == 'Doing'
    finally:
        kb.TASKS_FILE = original


def test_block_and_unblock(tmp_path):
    """kanban block/unblock round-trip."""
    tasks_file = tmp_path / 'tasks_source.json'
    tasks_file.write_text(json.dumps([
        {'id': 'T-2', 'title': 'blocker test', 'state': 'Doing'}
    ]))

    original = kb.TASKS_FILE
    kb.TASKS_FILE = tasks_file
    try:
        kb.cmd_block('T-2', '等待依赖')
        tasks = json.loads(tasks_file.read_text())
        assert tasks[0]['state'] == 'Blocked'
        assert tasks[0]['block'] == '等待依赖'
    finally:
        kb.TASKS_FILE = original


def test_create_auto_allocates_next_daily_id(tmp_path, monkeypatch, capsys):
    """create_auto should atomically allocate the next same-day JJC id."""
    tasks_file = tmp_path / 'tasks_source.json'
    tasks_file.write_text(json.dumps([
        {'id': 'JJC-20260308-001', 'title': 'old-1', 'state': 'Done'},
        {'id': 'JJC-20260308-002', 'title': 'old-2', 'state': 'Doing'},
        {'id': 'OTHER-001', 'title': 'ignore', 'state': 'Inbox'},
    ]))

    original = kb.TASKS_FILE
    original_today = kb._china_today_yyyymmdd
    kb.TASKS_FILE = tasks_file
    kb._china_today_yyyymmdd = lambda: '20260308'
    try:
        kb.cmd_create_auto('自动创建的新任务标题', 'Zhongshu', '中书省', '中书令', '太子整理旨意')
        out = capsys.readouterr().out.strip()
        assert out == 'JJC-20260308-003'
        tasks = json.loads(tasks_file.read_text())
        assert tasks[0]['id'] == 'JJC-20260308-003'
        assert tasks[0]['title'] == '自动创建的新任务标题'
    finally:
        kb.TASKS_FILE = original
        kb._china_today_yyyymmdd = original_today


def test_create_rejects_duplicate_task_id(tmp_path):
    """explicit create should reject duplicate task ids instead of overwriting."""
    tasks_file = tmp_path / 'tasks_source.json'
    tasks_file.write_text(json.dumps([
        {'id': 'JJC-20260308-001', 'title': 'existing', 'state': 'Done'}
    ]))

    original = kb.TASKS_FILE
    original_today = kb._china_today_yyyymmdd
    kb.TASKS_FILE = tasks_file
    kb._china_today_yyyymmdd = lambda: '20260308'
    try:
        with pytest.raises(kb.KanbanInputError):
            kb.cmd_create('JJC-20260308-001', '重复创建任务标题', 'Zhongshu', '中书省', '中书令')
        tasks = json.loads(tasks_file.read_text())
        assert tasks[0]['title'] == 'existing'
        assert tasks[0]['state'] == 'Done'
    finally:
        kb.TASKS_FILE = original
        kb._china_today_yyyymmdd = original_today


def test_terminal_jjc_task_cannot_reopen(tmp_path):
    """terminal JJC tasks must not be reopened via state changes."""
    tasks_file = tmp_path / 'tasks_source.json'
    tasks_file.write_text(json.dumps([
        {'id': 'JJC-20260308-001', 'title': 'done task', 'state': 'Done', 'org': '完成'}
    ]))

    original = kb.TASKS_FILE
    kb.TASKS_FILE = tasks_file
    try:
        with pytest.raises(kb.KanbanInputError):
            kb.cmd_state('JJC-20260308-001', 'Zhongshu', '试图重开已完成任务')
    finally:
        kb.TASKS_FILE = original
