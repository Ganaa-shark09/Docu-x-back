from apps.audit.services import AuditLogService
from apps.notifications.services import NotificationService
from apps.workflows.permissions.workflow_permissions import WorkflowPermissionService


def workflow_event(run, actor, event, *, definition=None, **metadata):
    resource = run or definition
    return AuditLogService.log(
        company=resource.company,
        user=actor,
        event_type="workflow_event",
        action=f"Workflow {event.replace('_', ' ')}.",
        resource_type="workflow_run" if run else "workflow_definition",
        resource_uuid=resource.uuid,
        metadata={"event": event, **metadata},
    )


def notify_workflow(run, actor, event, recipients=None):
    # Notifications contain no document titles, comments, evidence or outputs.
    recipients = recipients or [run.initiated_by]
    for recipient in {u.pk: u for u in recipients if u}.values():
        if WorkflowPermissionService.can(recipient, run, "view"):
            NotificationService.create_notification(
                company=run.company,
                recipient=recipient,
                actor=actor,
                title="Workflow update",
                message=f"Workflow {event.replace('_', ' ')}.",
                resource_type="workflow_run",
                resource_uuid=run.uuid,
                metadata={"event": event, "status": run.status},
            )
