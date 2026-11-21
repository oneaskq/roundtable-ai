from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('workspace', '0008_composerdraft')]

    operations = [
        migrations.AlterField(model_name='task', name='status',
            field=models.CharField(choices=[('draft', '草稿'), ('waiting', '待处理'), ('running', '处理中'),
                ('paused', '已暂停'), ('completed', '已完成'), ('failed', '失败'), ('cancelled', '已取消')],
                default='draft', max_length=16)),
        migrations.AddField(model_name='message', name='edited_from',
            field=models.ForeignKey(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name='edited_versions', to='workspace.message')),
        migrations.AddField(model_name='message', name='superseded_by',
            field=models.ForeignKey(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name='superseded_messages', to='workspace.message')),
        migrations.AddField(model_name='message', name='edit_request_id',
            field=models.UUIDField(editable=False, null=True, unique=True)),
        migrations.AddField(model_name='task', name='superseded_by_message',
            field=models.ForeignKey(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name='superseded_tasks', to='workspace.message')),
        migrations.AddField(model_name='task', name='edit_message',
            field=models.OneToOneField(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                       related_name='edit_task', to='workspace.message')),
    ]
