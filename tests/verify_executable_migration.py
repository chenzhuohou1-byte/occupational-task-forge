#!/usr/bin/env python3
"""显式实引擎验收：不自动跳过、逐项落盘；不调用 LLM。

python3 tests/verify_executable_migration.py
结果写 runs/executable-migration/verified.json（私有运行证据，不提交公开仓）。
"""
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pipeline import harness, qc
from pipeline.tasks import load_task


def main():
    evidence = ROOT / 'runs/executable-migration/verified.json'
    evidence.parent.mkdir(parents=True, exist_ok=True)
    rows = []

    def save(row):
        rows.append(row)
        evidence.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(row, ensure_ascii=False), flush=True)

    for relative in ('data_engineering/bids_session_auditor_e1',
                     'process_governance/coldchain_governance_g1'):
        task = load_task(str(ROOT / 'tasks' / relative))
        with tempfile.TemporaryDirectory(prefix='exec_verify_') as wd:
            harness.setup_env(task, wd)
            harness.golden_solver(task, wd)
            gold = harness.evaluate(task, wd)
            save({'task': task.task_id, 'kind': 'golden', 'score': gold.score,
                  'passed': gold.passed, 'fault': gold.fault, 'report': gold.raw})
            assert gold.passed and gold.score == 1.0 and not gold.fault
            if task.card['evaluation']['engine']['adapter'] == 'flowable':
                with patch.dict(os.environ, {'FLOWABLE_BASE': 'http://127.0.0.1:1/',
                                             'FLOWABLE_READY_TIMEOUT': '0'}):
                    fault = harness.evaluate(task, wd)
                save({'task': task.task_id, 'kind': 'unreachable', 'fault': fault.fault,
                      'passed': fault.passed})
                assert fault.fault and not fault.passed
        for name, verify in [('N6', qc.n6_verify), ('N8', qc.n8_floor_audit)]:
            ok, detail = verify(task)
            save({'task': task.task_id, 'kind': name, 'ok': ok, 'detail': detail})
            assert ok, detail
    print('真实引擎迁移验收全部通过；证据：' + str(evidence), flush=True)


if __name__ == '__main__':
    main()
