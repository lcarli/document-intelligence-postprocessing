from __future__ import annotations

from typing import Any

import pytest

from postprocessing.workflow import orchestrate


def test_parallel_fan_out_precedes_join() -> None:
    class Context:
        instance_id = "opaque-job-id"
        scheduled: list[tuple[str, Any]] = []
        joined = False

        def get_input(self) -> dict[str, Any]:
            return {"processors": [{"name": "first"}, {"name": "second"}]}

        def call_activity_with_retry(self, name: str, retry: Any, value: Any) -> Any:
            assert retry.max_number_of_attempts == 3
            self.scheduled.append((name, value))
            return len(self.scheduled)

        def task_all(self, tasks: list[Any]) -> Any:
            assert len(self.scheduled) == 2
            assert tasks == [1, 2]
            self.joined = True
            return "joined"

    context = Context()
    execution = orchestrate(context)
    assert next(execution) == "joined"
    assert context.joined
    assert execution.send([{"report_key": "first"}, {"report_key": "second"}]) == 3
    with pytest.raises(StopIteration) as stopped:
        execution.send({"status": "completed"})
    assert stopped.value.value == {"status": "completed"}
    assert context.scheduled[-1][0] == "aggregate_reports"
    assert all(set(item[1]) <= {"job_id", "processor"} for item in context.scheduled)
