from django.urls import re_path

from . import source_views, views

UID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

urlpatterns = [
    re_path(r"^drafts/?$", views.composer_drafts, name="composer-drafts"),
    re_path(r"^projects/?$", views.projects, name="projects"),
    re_path(rf"^projects/(?P<project_id>{UID})/?$", views.project_detail, name="project-detail"),
    re_path(rf"^projects/(?P<project_id>{UID})/export/?$", views.project_export, name="project-export"),
    re_path(r"^conversations/?$", views.conversations, name="conversations"),
    re_path(rf"^conversations/(?P<conversation_id>{UID})/?$", views.conversation_detail, name="conversation-detail"),
    re_path(rf"^messages/(?P<message_id>{UID})/edit/?$", views.edit_message, name="message-edit"),
    re_path(rf"^conversations/(?P<conversation_id>{UID})/acceptance-cases/?$", views.acceptance_cases, name="acceptance-cases"),
    re_path(rf"^conversations/(?P<conversation_id>{UID})/sources/?$", source_views.conversation_sources, name="conversation-sources"),
    re_path(rf"^draft-sources/(?P<conversation_id>{UID})/?$", source_views.conversation_sources, {'draft': True}, name="draft-sources"),
    re_path(rf"^sources/(?P<source_id>{UID})/?$", source_views.source_detail, name="source-detail"),
    re_path(r"^memories/?$", views.memories, name="memories"),
    re_path(rf"^memories/(?P<memory_id>{UID})/?$", views.memory_detail, name="memory-detail"),
    re_path(r"^data/overview/?$", views.overview, name="data-overview"),
    re_path(r"^data/export/?$", views.personal_export, name="data-export"),
    re_path(r"^data/policy/?$", views.policy, name="data-policy"),
    re_path(r"^data/delete-account/?$", views.delete_account, name="delete-account"),
    re_path(r"^data/deletion-backups/?$", views.deletion_backup_status, name="deletion-backup-status"),
    re_path(r"^data/deletion-backups/ledger/?$", views.deletion_ledger_download, name="deletion-ledger-download"),
    re_path(r"^data/deletion-requests/?$", views.deletion_requests, name="deletion-requests"),
    re_path(rf"^data/deletion-requests/(?P<request_id>{UID})/cancel/?$", views.cancel_deletion, name="cancel-deletion"),
]
