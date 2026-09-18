from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("projects", "0070_suite_average_durations"),
    ]

    operations = [
        migrations.AlterField(
            model_name="test",
            name="maintainer",
            field=models.ForeignKey(
                blank=True,
                help_text="Person responsible for maintaining this test",
                null=True,
                on_delete=models.SET_NULL,
                related_name="maintained_tests",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="test",
            name="disabled_user",
            field=models.ForeignKey(
                blank=True,
                help_text="Person who last updated this override behavior",
                null=True,
                on_delete=models.SET_NULL,
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
