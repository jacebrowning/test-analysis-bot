from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("projects", "0067_longer_test_names"),
    ]

    operations = [
        migrations.AddField(
            model_name="test",
            name="maintainer",
            field=models.ForeignKey(
                blank=True,
                help_text="User responsible for maintaining this test",
                null=True,
                on_delete=models.SET_NULL,
                related_name="maintained_tests",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
