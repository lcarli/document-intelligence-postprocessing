from __future__ import annotations

from collections.abc import Generator
from typing import Any, Protocol

import azure.durable_functions as df


class Context(Protocol):
    instance_id: str

    def get_input(self) -> dict[str, Any]: ...
    def call_activity_with_retry(self, name: str, retry: Any, value: Any) -> Any: ...
    def task_all(self, tasks: list[Any]) -> Any: ...


def orchestrate(context: Context) -> Generator[Any, Any, dict[str, str]]:
    payload = context.get_input()
    retry = df.RetryOptions(
        first_retry_interval_in_milliseconds=2000,
        max_number_of_attempts=3,
    )
    tasks = [
        context.call_activity_with_retry(
            "run_processor",
            retry,
            {"job_id": context.instance_id, "processor": processor},
        )
        for processor in payload["processors"]
    ]
    yield context.task_all(tasks)
    return (
        yield context.call_activity_with_retry(
            "aggregate_reports", retry, {"job_id": context.instance_id}
        )
    )
