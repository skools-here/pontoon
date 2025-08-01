import logging

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST

from pontoon.base.models import (
    Entity,
    Locale,
    Project,
    TranslatedResource,
    Translation,
    TranslationMemoryEntry,
)
from pontoon.base.utils import (
    readonly_exists,
    require_AJAX,
)
from pontoon.batch import forms
from pontoon.batch.actions import ACTIONS_FN_MAP


log = logging.getLogger(__name__)


def update_translation_memory(changed_translation_pks, project, locale):
    """Update translation memory for a list of translations."""
    memory_entries = [
        TranslationMemoryEntry(
            source=t.tm_source,
            target=t.tm_target,
            locale=locale,
            entity=t.entity,
            translation=t,
            project=project,
        )
        for t in (
            Translation.objects.filter(pk__in=changed_translation_pks).prefetch_related(
                "entity__resource"
            )
        )
    ]
    TranslationMemoryEntry.objects.bulk_create(memory_entries)


@login_required(redirect_field_name="", login_url="/403")
@require_POST
@require_AJAX
@transaction.atomic
def batch_edit_translations(request):
    """Perform an action on a list of translations.

    Available actions are defined in `ACTIONS_FN_MAP`. Arguments to this view
    are defined in `models.BatchActionsForm`.

    """
    form = forms.BatchActionsForm(request.POST)
    if not form.is_valid():
        return JsonResponse(
            {
                "status": False,
                "message": "{error}".format(
                    error=form.errors.as_json(escape_html=True)
                ),
            },
            status=400,
        )

    locale = get_object_or_404(Locale, code=form.cleaned_data["locale"])
    entities = Entity.objects.filter(pk__in=form.cleaned_data["entities"])

    if not entities.exists():
        return JsonResponse({"count": 0})

    # Batch editing is only available to translators. Check if user has
    # translate permissions for all of the projects in passed entities.
    # Also make sure projects are not enabled in read-only mode for a locale.
    projects_pk = entities.values_list("resource__project__pk", flat=True)
    projects = Project.objects.filter(pk__in=projects_pk.distinct())

    for project in projects:
        if not request.user.can_translate(
            project=project, locale=locale
        ) or readonly_exists(projects, locale):
            return JsonResponse(
                {
                    "status": False,
                    "message": "Forbidden: You don't have permission for batch editing.",
                },
                status=403,
            )

    active_translations = Translation.objects.filter(
        active=True,
        locale=locale,
        entity__in=entities,
    )

    # Execute the actual action.
    action_function = ACTIONS_FN_MAP[form.cleaned_data["action"]]
    action_status = action_function(
        form,
        request.user,
        active_translations,
        locale,
    )

    if action_status.get("error"):
        return JsonResponse(action_status)

    invalid_translation_count = len(action_status.get("invalid_translation_pks", []))
    if action_status["count"] == 0:
        return JsonResponse(
            {
                "count": 0,
                "invalid_translation_count": invalid_translation_count,
                "badge_update": action_status["badge_update"],
            }
        )

    tr_pks = [tr.pk for tr in action_status["translated_resources"]]
    TranslatedResource.objects.filter(pk__in=tr_pks).calculate_stats()

    # Mark translations as changed
    active_translations.bulk_mark_changed()

    # Reset term translations for entities belonging to the Terminology project
    changed_entity_pks = [entity.pk for entity in action_status["changed_entities"]]
    terminology_entities = Entity.objects.filter(
        pk__in=changed_entity_pks,
        resource__project__slug="terminology",
    )

    for e in terminology_entities:
        e.reset_term_translation(locale)

    # Update latest translation.
    if action_status["latest_translation_pk"]:
        Translation.objects.get(
            pk=action_status["latest_translation_pk"]
        ).update_latest_translation()

    update_translation_memory(action_status["changed_translation_pks"], project, locale)

    return JsonResponse(
        {
            "count": action_status["count"],
            "invalid_translation_count": invalid_translation_count,
            "badge_update": action_status["badge_update"],
        }
    )
