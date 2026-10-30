"""受控模型桩的本机并发测量；不作为真实模型吞吐或生产容量结论。"""

import time

import pytest

from tests_async.test_worker_flow import assert_refunded_once


@pytest.mark.parametrize("concurrency", [1, 4])
def test_worker_batch_measurement(app, processes, concurrency, record_property):
    """同样四张工单分别串行、并发执行，记录包含进程启动的端到端耗时。"""
    cases = [app.scenario() for _ in range(4)]
    for case in cases:
        app.submit(case)
    started = time.perf_counter()
    processes.worker(concurrency=concurrency, delay=0.25)
    for case in cases:
        assert_refunded_once(app, case)
    elapsed = time.perf_counter() - started
    record_property("measurement_scope", "local_stub_includes_worker_startup")
    record_property("concurrency", concurrency)
    record_property("tickets", len(cases))
    record_property("stub_delay_seconds_per_model_call", 0.25)
    record_property("batch_elapsed_seconds", round(elapsed, 4))
    record_property("tickets_per_second", round(len(cases) / elapsed, 4))
