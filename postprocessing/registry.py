from postprocessing.models import ProcessingError, ProcessorSpec
from postprocessing.processors.base import Processor
from postprocessing.processors.grayscale import GrayscaleCheckboxProcessor


def processors() -> tuple[Processor, ...]:
    return (GrayscaleCheckboxProcessor(),)


def registered_specs() -> list[ProcessorSpec]:
    result = [processor.spec for processor in processors()]
    if len({spec.name for spec in result}) != len(result) or not 1 <= len(result) <= 8:
        raise ProcessingError("invalid_processor_registry")
    return result


def resolve(spec: ProcessorSpec) -> Processor:
    for processor in processors():
        if processor.spec == spec:
            return processor
    raise ProcessingError("processor_version_unavailable")
