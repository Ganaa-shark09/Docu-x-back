from django.db import migrations


def seed(apps, schema_editor):
    Category = apps.get_model("workflows", "HRPolicyCategory")
    for code, name in (
        ("leave", "Leave policies"),
        ("working_time", "Working time"),
        ("remote_work", "Remote work"),
        ("conduct", "Code of conduct"),
        ("travel", "Travel and expenses"),
        ("benefits_policy", "Benefits policies"),
        ("health_safety", "Health and safety"),
        ("other", "Other HR policies"),
    ):
        Category.objects.get_or_create(
            company=None, kind="policy", code=code, defaults={"name": name}
        )


class Migration(migrations.Migration):
    dependencies = [
        ("workflows", "0011_hrpolicycategory_hrpolicy_hrpolicysettings_and_more")
    ]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
