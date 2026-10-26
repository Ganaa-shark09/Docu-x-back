from dataclasses import dataclass, field

from rest_framework.exceptions import ValidationError


@dataclass
class ProcessingResult:
    data: dict
    evidence: list = field(default_factory=list)
    exception_category: str | None = None


class WorkflowProcessingService:
    """Internal, server-registered domain processors; never remote integrations."""

    adapters = {}

    @classmethod
    def adapter(cls, name):
        if not isinstance(name, str) or name not in cls.adapters:
            raise ValidationError("Workflow processor is not registered.")
        return cls.adapters[name]

    @classmethod
    def validate(cls, company_id, config):
        adapter = cls.adapter(config.get("adapter"))
        adapter.validate_config(
            company_id, config.get("operation"), config.get("options", {})
        )

    @classmethod
    def processors(cls, version):
        seen = set()
        for step in version.steps.filter(step_type="processing"):
            name = step.configuration["adapter"]
            if name not in seen:
                seen.add(name)
                yield cls.adapter(name), step.configuration.get("options", {})

    @classmethod
    def validate_start(cls, user, version, documents):
        for adapter, options in cls.processors(version):
            if hasattr(adapter, "validate_version"):
                adapter.validate_version(version, options)
            adapter.validate_start(user, documents, options)

    @classmethod
    def validate_command(cls, user, run, action):
        for adapter, options in cls.processors(run.version):
            adapter.validate_command(user, run, action, options)
