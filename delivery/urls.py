from django.urls import path
from . import views
from . import team_views
from . import quality_views

urlpatterns = [
    path('capabilities', views.capabilities),
    path('capabilities/', views.capabilities),
    path('release-readiness', views.release_readiness),
    path('quality/cases', quality_views.cases),
    path('quality/rounds', quality_views.rounds),
    path('quality/rounds/<uuid:round_id>', quality_views.round_detail_view),
    path('quality/rounds/<uuid:round_id>/samples', quality_views.add_sample),
    path('quality/rounds/<uuid:round_id>/start', quality_views.start_review),
    path('quality/samples/<uuid:sample_id>/review', quality_views.review_sample),
    path('tasks/<uuid:task_id>/transition', views.transition),
    path('tasks/<uuid:task_id>/artifacts', views.artifacts),
    path('tasks/<uuid:task_id>/brief', views.brief),
    path('artifacts/<uuid:artifact_id>/download', views.download),
    path('artifacts/<uuid:artifact_id>/detail', views.artifact_detail),
    path('artifacts/<uuid:artifact_id>/files/<int:index>', views.artifact_file),
    path('conversations/<uuid:conversation_id>/artifacts', views.conversation_artifacts),
    path('conversations/<uuid:conversation_id>/respond', views.respond),
    path('conversations/<uuid:conversation_id>/submissions/<uuid:request_id>', team_views.submission_receipt),
    path('conversations/<uuid:conversation_id>/runs/<uuid:run_id>/status', team_views.run_status),
    path('conversations/<uuid:conversation_id>/collaborate', team_views.collaborate),
    path('conversations/<uuid:conversation_id>/confirm-decision', team_views.confirm_decision_brief),
    path('conversations/<uuid:conversation_id>/team-messages', team_views.team_message),
]
